"""PCL-style Minecraft mod batch analyzer."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict, deque
from dataclasses import dataclass, field
from os import path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
import json
import os
import re
import threading
import time
import zipfile

try:
    from lxml import etree
except ImportError:
    etree = None

import requests
try:
    import xlwings as xw
except ImportError:
    xw = None


MAX_RETRIES = 3
REQUEST_TIMEOUT = (5, 15)
MAX_MOD_WORKERS = 4
GROUP_SIZE = 20
QUEUE_WORKERS = {"MC百科": 2, "Modrinth": 4, "Bing": 2, "详情": 4}
RATE_LIMITS = {
    "mcmod.cn": {"per_second": 1, "per_minute": 30},
    "modrinth.com": {"per_second": 5, "per_minute": 240},
    "bing.com": {"per_second": 1, "per_minute": 30},
    "curseforge.com": {"per_second": 1, "per_minute": 30},
}
# 保留旧配置名，兼容外部调用和旧测试；未知站点使用此值作为每秒上限。
MAX_REQUESTS_PER_SECOND = 50
RETRY_BACKOFF_SECONDS = 1
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
OUTPUT_HEADERS = [
    "序号", "Mod 文件名", "Mod ID", "名称", "中文名", "作者", "Minecraft 版本",
    "加载器", "客户端安装", "服务端安装", "简介", "来源链接", "匹配置信度", "查询状态",
]
LOADERS = ("Forge", "NeoForge", "Fabric", "Quilt")
SOURCE_DOMAINS = {"MC百科": "mcmod.cn", "Modrinth": "modrinth.com", "CurseForge": "curseforge.com"}
SOURCE_SEARCH_SCOPES = {
    "MC百科": "mcmod.cn",
    "Modrinth": "modrinth.com",
    "CurseForge": "curseforge.com/minecraft/mc-mods",
}
SHEET_COLUMN_WIDTHS = {
    "A:A": 7, "B:B": 34, "C:C": 22, "D:D": 24, "E:E": 20, "F:F": 18,
    "G:G": 20, "H:H": 12, "I:I": 14, "J:J": 14, "K:K": 48, "L:L": 48,
    "M:M": 18, "N:N": 36,
}
WRAPPED_COLUMNS = {"B:B", "K:K", "L:L", "N:N"}


@dataclass
class JarMetadata:
    mod_ids: List[str] = field(default_factory=list)
    names: List[str] = field(default_factory=list)
    authors: List[str] = field(default_factory=list)
    description: str = ""
    versions: List[str] = field(default_factory=list)
    loader: str = "未知"
    error: str = ""

    @property
    def multiple(self):
        return len(self.mod_ids) > 1


@dataclass
class SearchCandidate:
    source: str
    url: str
    name: str = ""
    slug: str = ""
    project_id: str = ""
    chinese_name: str = ""
    authors: List[str] = field(default_factory=list)
    description: str = ""
    versions: List[str] = field(default_factory=list)
    loaders: List[str] = field(default_factory=list)
    client_side: str = ""
    server_side: str = ""
    score: int = 0
    confirmed: bool = False
    source_urls: List[str] = field(default_factory=list)


@dataclass
class SearchResult:
    source: str
    status: str = "未找到"
    candidates: List[SearchCandidate] = field(default_factory=list)
    error: str = ""

    @property
    def failed(self) -> bool:
        return self.status == "请求失败"


@dataclass
class ModRecord:
    filename: str
    metadata: JarMetadata
    candidate: Optional[SearchCandidate] = None
    candidates: List[SearchCandidate] = field(default_factory=list)
    confidence: str = "未找到"
    status: List[str] = field(default_factory=list)
    failure_reason: str = ""


@dataclass
class FileComponent:
    filename: str
    mod_directory: str
    group_index: int
    order: int


@dataclass
class MetadataComponent:
    metadata: JarMetadata


@dataclass
class SearchComponent:
    terms: List[str] = field(default_factory=list)
    phase: str = "待读取"
    results: Dict[str, SearchResult] = field(default_factory=dict)


@dataclass
class CandidateComponent:
    candidates: List[SearchCandidate] = field(default_factory=list)


@dataclass
class RecordComponent:
    record: ModRecord


@dataclass
class ErrorComponent:
    stage: str
    error_type: str
    reason: str


class ECSWorld:
    """单文件工具使用的最小 ECS：实体只由组件组成，系统负责推进状态。"""

    def __init__(self):
        self._next_entity = 1
        self._components = defaultdict(dict)

    def create_entity(self, *components: object) -> int:
        entity_id = self._next_entity
        self._next_entity += 1
        for component in components:
            self.add(entity_id, component)
        return entity_id

    def add(self, entity_id: int, component: object):
        self._components[type(component)][entity_id] = component
        return component

    def get(self, entity_id: int, component_type: type, default=None):
        return self._components.get(component_type, {}).get(entity_id, default)

    def has(self, entity_id: int, component_type: type) -> bool:
        return entity_id in self._components.get(component_type, {})

    def entities_with(self, *component_types: type) -> List[int]:
        if not component_types:
            return []
        entity_ids = set(self._components.get(component_types[0], {}))
        for component_type in component_types[1:]:
            entity_ids.intersection_update(self._components.get(component_type, {}))
        return sorted(entity_ids)


class WindowRateLimiter:
    """线程安全的每秒 + 每分钟滑动窗口限流器。"""

    def __init__(self, per_second: int, per_minute: int):
        self.per_second = max(0, int(per_second))
        self.per_minute = max(0, int(per_minute))
        self._events = deque()
        self._lock = threading.Lock()

    def acquire(self):
        while True:
            wait = 0.0
            with self._lock:
                now = time.monotonic()
                while self._events and now - self._events[0] >= 60:
                    self._events.popleft()
                second_count = sum(event > now - 1 for event in self._events)
                minute_count = len(self._events)
                if self.per_second and second_count >= self.per_second:
                    wait = max(wait, 1 - (now - next(event for event in self._events if event > now - 1)))
                if self.per_minute and minute_count >= self.per_minute:
                    wait = max(wait, 60 - (now - self._events[0]))
                if wait <= 0:
                    self._events.append(now)
                    return
            time.sleep(wait)


class SearchPipeline:
    """一个分组共享的四队列协调器。"""

    def __init__(self, manager: "Manager", group_index: int = 0, total_groups: int = 0):
        self.manager = manager
        self.group_index = group_index
        self.total_groups = total_groups
        self.queues = {}

    def __enter__(self):
        self.queues = {
            name: ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mod-" + name)
            for name, workers in QUEUE_WORKERS.items()
        }
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        for executor in self.queues.values():
            executor.shutdown(wait=True)
        self.queues.clear()

    def process(self, filename: str, metadata: JarMetadata, on_phase=None, on_results=None) -> ModRecord:
        terms = derive_search_terms(filename, metadata)
        if on_phase:
            on_phase("直接搜索")
        direct = {}
        self.manager.log_pipeline(self.group_index, self.total_groups, "MC百科", "开始", filename)
        self.manager.log_pipeline(self.group_index, self.total_groups, "Modrinth", "开始", filename)
        futures = {
            "MC百科": self.queues["MC百科"].submit(self.manager.search_mcmod, terms, filename),
            "Modrinth": self.queues["Modrinth"].submit(self.manager.search_modrinth, terms, filename),
        }
        for source, future in futures.items():
            try:
                direct[source] = future.result()
            except Exception as error:
                direct[source] = SearchResult(source, "请求失败", error=type(error).__name__)
            self.manager.log_pipeline(
                self.group_index, self.total_groups, source, "完成", filename, direct[source].status
            )

        missing = [source for source, result in direct.items() if not result.candidates]
        missing = unique(missing + ["CurseForge"])
        if on_phase:
            on_phase("Bing 发现")
        self.manager.log_pipeline(
            self.group_index, self.total_groups, "Bing", "开始", filename,
            "目标=" + ",".join(missing),
        )
        bing_future = self.queues["Bing"].submit(self.manager.discover_bing, missing, terms, filename)
        try:
            discovered = bing_future.result()
        except Exception as error:
            discovered = {source: SearchResult(source, "请求失败", error=type(error).__name__) for source in missing}
        self.manager.log_pipeline(
            self.group_index, self.total_groups, "Bing", "完成", filename,
            "发现=" + str(sum(len(result.candidates) for result in discovered.values())),
        )

        results = dict(direct)
        bing_status = "已发现" if any(result.candidates for result in discovered.values()) else (
            "请求失败" if discovered and all(result.failed for result in discovered.values()) else "未找到"
        )
        results["Bing"] = SearchResult("Bing", bing_status)
        for source, result in discovered.items():
            target = results.setdefault(source, SearchResult(source))
            target.candidates.extend(result.candidates)
            target.candidates = self.manager.deduplicate(target.candidates)
            if result.candidates:
                target.status = "已发现"
            elif target.status != "已发现" and result.failed:
                target.status = "请求失败"
        for source in ("MC百科", "Modrinth", "CurseForge"):
            results.setdefault(source, SearchResult(source))
        if on_results:
            on_results(results)
        if on_phase:
            on_phase("详情确认")
        detail_count = sum(len(result.candidates) for result in results.values())
        self.manager.log_pipeline(
            self.group_index, self.total_groups, "详情", "开始", filename,
            "候选=" + str(detail_count),
        )
        candidates = self.manager.confirm_candidates(
            results, metadata, filename, detail_executor=self.queues["详情"]
        )
        self.manager.log_pipeline(
            self.group_index, self.total_groups, "详情", "完成", filename,
            "确认=" + str(len(candidates)),
        )
        if on_phase:
            on_phase("汇总完成")
        return self.manager.build_record(filename, metadata, candidates, results)


def normalize_identifier(value: str) -> str:
    return re.sub(r"[ _-]+", "", (value or "").strip().casefold())


def unique(values: Iterable[str]) -> List[str]:
    result = []
    for value in values:
        value = str(value or "").strip()
        if value and value not in result:
            result.append(value)
    return result


def map_side(value: str, side: str) -> str:
    return {"required": side + "需装", "optional": side + "可选", "unsupported": side + "无效"}.get((value or "").lower(), "未知")


def derive_search_terms(filename: str, metadata: JarMetadata) -> List[str]:
    stem = path.splitext(path.basename(filename))[0]
    stem = re.sub(r"^\[[^]]+\]\s*", "", stem)
    stem = re.sub(r"【[^】]+】", "", stem).strip()
    stem = re.sub(r"^[\u4e00-\u9fff]+[ _.-]+", "", stem)
    clean = re.sub(r"(?:[-+_]?(?:mc)?\d+(?:\.\d+){1,3})", "", stem, flags=re.I)
    clean = re.sub(r"[-+_]?(?:neo)?forge|[-+_]?(?:fabric|quilt)", "", clean, flags=re.I)
    primary_id = metadata.mod_ids[:1]
    primary_name = metadata.names[:1]
    other_names = metadata.names[1:]
    return unique(primary_id + primary_name + [clean.strip(" _-"), stem] + other_names)[:5]


def extract_versions(value: str) -> List[str]:
    return unique(re.findall(r"(?:1\.\d+(?:\.\d+)?|\d+\.\d+(?:\.\d+)?)", value or ""))


def versions_from_text(content: str) -> List[str]:
    return extract_versions(content)


def dependency_values(value) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for entry in value for item in dependency_values(entry)]
    if isinstance(value, dict):
        for key in ("versionRange", "versions", "version"):
            if key in value:
                return dependency_values(value[key])
    return []


def toml_minecraft_versions(content: str) -> List[str]:
    versions = []
    blocks = re.split(r"\[\[dependencies\.[^]]+]]", content)[1:]
    for block in blocks:
        mod_id = re.search(r"^\s*modId\s*=\s*[\"']([^\"']+)", block, re.M)
        if not mod_id or mod_id.group(1).casefold() != "minecraft":
            continue
        version_range = re.search(r"^\s*versionRange\s*=\s*[\"']([^\"']+)", block, re.M)
        if version_range:
            versions.append(version_range.group(1))
    return unique(versions)


def json_minecraft_versions(data: dict, loader: str) -> List[str]:
    root = data.get("quilt_loader", {}) if loader == "Quilt" else data
    dependencies = root.get("depends", {}) if isinstance(root, dict) else {}
    if isinstance(dependencies, dict):
        return unique(dependency_values(dependencies.get("minecraft", [])))
    if isinstance(dependencies, list):
        values = []
        for dependency in dependencies:
            if isinstance(dependency, dict) and str(dependency.get("id", "")).casefold() == "minecraft":
                values.extend(dependency_values(dependency))
        return unique(values)
    return []


def source_url_host(url: str) -> str:
    match = re.match(r"https?://([^/]+)", url or "", re.I)
    return re.sub(r"^www\.", "", match.group(1).lower()) if match else ""


def is_allowed_url(url: str, source: str) -> bool:
    host = source_url_host(url)
    domain = SOURCE_DOMAINS[source]
    return host == domain or host.endswith("." + domain)


class Manager:
    def __init__(self, mod_directory: str):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self._last_request_time = None
        self._request_lock = threading.Lock()
        self._limiters = {}
        self._limiters_lock = threading.Lock()
        self._active_pipeline = None
        self._entity_local = threading.local()
        self._log_lock = threading.Lock()
        try:
            self.analyze_directory(mod_directory)
        finally:
            self.session.close()

    def analyze_directory(self, mod_directory: str):
        if xw is None:
            raise RuntimeError("生成 Excel 需要安装 xlwings")
        files = os.listdir(mod_directory)
        jar_files = sorted(filename for filename in files if filename.lower().endswith(".jar"))
        skipped_files = [filename for filename in files if not filename.lower().endswith(".jar")]
        print(f"[开始] 发现 {len(jar_files)} 个 JAR，准备分析。")
        for filename in skipped_files:
            print(f"[跳过] {filename}: 非 JAR 文件。")
        workbook = xw.Book()
        sheet = workbook.sheets["Sheet1"]
        sheet.range(1, 1).value = OUTPUT_HEADERS
        row = 1
        completed = 0
        groups = [jar_files[index:index + GROUP_SIZE] for index in range(0, len(jar_files), GROUP_SIZE)]
        total_groups = len(groups)
        for group_number, group in enumerate(groups, 1):
            print(f"[组] 开始第 {group_number}/{total_groups} 组，共 {len(group)} 个 JAR。")
            world = ECSWorld()
            entities = []
            for order, filename in enumerate(group):
                try:
                    metadata = self.read_jar_metadata(path.join(mod_directory, filename))
                except Exception as error:
                    metadata = JarMetadata(error=type(error).__name__)
                entity_id = world.create_entity(
                    FileComponent(filename, mod_directory, group_number, order),
                    MetadataComponent(metadata),
                    SearchComponent(),
                )
                if metadata.error:
                    world.add(entity_id, ErrorComponent("元数据", metadata.error, metadata.error))
                entities.append(entity_id)

            with SearchPipeline(self, group_number, total_groups) as pipeline:
                self._active_pipeline = pipeline
                try:
                    with ThreadPoolExecutor(max_workers=MAX_MOD_WORKERS, thread_name_prefix="mod-实体") as pool:
                        futures = {pool.submit(self._run_entity, world, entity_id): entity_id for entity_id in entities}
                        for future in as_completed(futures):
                            entity_id = futures[future]
                            try:
                                future.result()
                            except Exception as error:
                                component = world.get(entity_id, FileComponent)
                                metadata = world.get(entity_id, MetadataComponent).metadata
                                record = self._failed_record(component.filename, metadata, error)
                                world.add(entity_id, RecordComponent(record))
                                world.add(entity_id, ErrorComponent("实体", type(error).__name__, str(error)))
                            row, completed = self.write_entity_result(sheet, row, completed, world, entity_id)
                finally:
                    self._active_pipeline = None

            print(f"[队列] 第 {group_number}/{total_groups} 组四阶段队列已完成。")
            print(f"[组] 第 {group_number}/{total_groups} 组完成，已处理 {completed} 个 JAR。")
        self.format_sheet(sheet, row)
        workbook.save("result.xlsx")
        print(f"[完成] 已处理 {completed} 个 JAR，结果已保存至 result.xlsx。")

    def write_entity_result(self, sheet, row: int, completed: int, world: ECSWorld, entity_id: int) -> Tuple[int, int]:
        """由主线程在实体完成时立即写入，避免 Excel 的跨线程访问。"""
        file_component = world.get(entity_id, FileComponent)
        metadata = world.get(entity_id, MetadataComponent).metadata
        record_component = world.get(entity_id, RecordComponent)
        record = record_component.record if record_component else self._failed_record(
            file_component.filename, metadata, RuntimeError("实体未完成")
        )
        row += 1
        if metadata.error:
            print(f"[元数据异常] {file_component.filename}: {metadata.error}。")
        elif metadata.multiple:
            print(f"[元数据提示] {file_component.filename}: 多 Mod JAR，需人工审核。")
        if record.failure_reason:
            print(f"[文件失败] {file_component.filename}: {record.failure_reason}。")
        sheet.range(row, 1).value = self.record_values(row - 1, record)
        self.log_record(record)
        return row, completed + 1

    def _run_entity(self, world: ECSWorld, entity_id: int):
        file_component = world.get(entity_id, FileComponent)
        metadata_component = world.get(entity_id, MetadataComponent)
        search_component = world.get(entity_id, SearchComponent)
        self._entity_local.entity_id = entity_id
        try:
            search_component.terms = derive_search_terms(file_component.filename, metadata_component.metadata)
            pipeline = getattr(self, "_active_pipeline", None)
            if pipeline is None:
                record = self.analyze_jar(file_component.filename, metadata_component.metadata)
            else:
                record = pipeline.process(
                    file_component.filename, metadata_component.metadata,
                    on_phase=lambda phase: setattr(search_component, "phase", phase),
                    on_results=lambda results: search_component.results.update(results),
                )
            world.add(entity_id, CandidateComponent(record.candidates))
            world.add(entity_id, RecordComponent(record))
            search_component.phase = "待写入"
        except Exception as error:
            record = self._failed_record(file_component.filename, metadata_component.metadata, error)
            world.add(entity_id, ErrorComponent("实体", type(error).__name__, str(error)))
            world.add(entity_id, RecordComponent(record))
            search_component.phase = "完成（失败）"
        finally:
            try:
                del self._entity_local.entity_id
            except AttributeError:
                pass

    def analyze_file(self, mod_directory: str, filename: str) -> Tuple[JarMetadata, ModRecord]:
        """在线程工作任务中读取并分析一个 JAR。"""
        metadata = JarMetadata()
        try:
            metadata = self.read_jar_metadata(path.join(mod_directory, filename))
            record = self.analyze_jar(filename, metadata)
        except Exception as error:
            record = self._failed_record(filename, metadata, error)
        return metadata, record

    @staticmethod
    def _failed_record(filename: str, metadata: JarMetadata, error: Exception) -> ModRecord:
        return ModRecord(
            filename=filename,
            metadata=metadata,
            confidence="请求失败",
            status=["MC百科=请求失败", "Modrinth=请求失败", "Bing=请求失败", "CurseForge=请求失败"],
            failure_reason=type(error).__name__,
        )

    @staticmethod
    def log_record(record: ModRecord):
        metadata = record.metadata
        local = ", ".join(filter(None, ["/".join(metadata.mod_ids), metadata.loader, "/".join(metadata.versions)])) or "未识别本地元数据"
        result_name = record.candidate.name if record.candidate else "未找到可信候选"
        print(f"[结果] {record.filename} -> {local} -> {result_name} | {record.confidence} | {'；'.join(record.status)}")

    def log_pipeline(self, group_index: int, total_groups: int, queue_name: str, event: str, filename: str, detail: str = ""):
        """输出组内队列进度；锁保证并发线程的一条日志不会互相穿插。"""
        if group_index <= 0:
            return
        if not hasattr(self, "_log_lock"):
            self._log_lock = threading.Lock()
        group = f"{group_index}/{total_groups}" if total_groups else str(group_index)
        suffix = f" | {detail}" if detail else ""
        with self._log_lock:
            print(f"[队列] 组 {group} {queue_name} {event}: {filename}{suffix}")

    @staticmethod
    def record_values(index: int, record: ModRecord) -> List[str]:
        metadata = record.metadata
        candidate = record.candidate
        mcmod_candidate = next((item for item in record.candidates if item.source == "MC百科"), None)
        install_candidate = mcmod_candidate or candidate
        client = map_side(install_candidate.client_side, "客户端") if install_candidate and install_candidate.client_side else "未知"
        server = map_side(install_candidate.server_side, "服务端") if install_candidate and install_candidate.server_side else "未知"
        if mcmod_candidate:
            client = mcmod_candidate.client_side or client
            server = mcmod_candidate.server_side or server
        name = candidate.name if candidate else (metadata.names[0] if metadata.names else "")
        author = "\n".join(metadata.authors) or ("\n".join(candidate.authors) if candidate else "")
        description = metadata.description or (candidate.description if candidate else "")
        links = "\n".join(unique([url for item in record.candidates for url in ([item.url] + item.source_urls) if url]))
        return [
            index, record.filename, "\n".join(metadata.mod_ids), name,
            mcmod_candidate.chinese_name if mcmod_candidate else (candidate.chinese_name if candidate else ""), author,
            "\n".join(metadata.versions), metadata.loader, client, server,
            description, links, record.confidence, "\n".join(record.status),
        ]

    @staticmethod
    def format_sheet(sheet, last_row: int):
        used = sheet.range(1, 1).resize(last_row, len(OUTPUT_HEADERS))
        used.api.WrapText = True
        for column, width in SHEET_COLUMN_WIDTHS.items():
            column_range = sheet.range(column)
            column_range.column_width = width
            column_range.api.WrapText = column in WRAPPED_COLUMNS
        sheet.range("1:1").api.Font.Bold = True
        sheet.range("1:1").row_height = 24
        for row in range(2, last_row + 1):
            values = sheet.range(row, 1).resize(1, len(OUTPUT_HEADERS)).value
            sheet.range(row, 1).row_height = Manager.calculate_row_height(values)

    @staticmethod
    def calculate_row_height(values: Sequence[object]) -> float:
        max_lines = 1
        for index, value in enumerate(values):
            text = str(value or "")
            column = chr(ord("A") + index) + ":" + chr(ord("A") + index)
            width = SHEET_COLUMN_WIDTHS[column]
            lines = sum(max(1, (len(part) + max(1, int(width * 1.6)) - 1) // max(1, int(width * 1.6))) for part in text.splitlines() or [""])
            max_lines = max(max_lines, lines)
        return min(300, max(20, 16 * max_lines + 4))

    def analyze_jar(self, filename: str, metadata: JarMetadata) -> ModRecord:
        if getattr(self, "_active_pipeline", None) is not None:
            return self._active_pipeline.process(filename, metadata)
        with SearchPipeline(self) as pipeline:
            return pipeline.process(filename, metadata)

    def search_mcmod(self, terms: Sequence[str], filename: str) -> SearchResult:
        candidates = []
        completed = False
        for term in terms:
            response = self.request("https://search.mcmod.cn/s", "MC百科搜索", filename, {"key": term, "filter": 1})
            if response is None:
                continue
            completed = True
            if etree is None:
                continue
            tree = etree.HTML(response.text)
            for element in tree.xpath("//div[contains(@class,'result-item')]//a[@target='_blank']"):
                url = element.get("href", "")
                if url and is_allowed_url(url, "MC百科"):
                    candidates.append(SearchCandidate("MC百科", url, " ".join(element.xpath(".//text()")).strip(), slug=self.url_slug(url)))
        status = "已发现" if candidates else ("未找到" if completed else "请求失败")
        return SearchResult("MC百科", status, self.deduplicate(candidates))

    def search_modrinth(self, terms: Sequence[str], filename: str) -> SearchResult:
        candidates = []
        completed = False
        for term in terms:
            response = self.request("https://api.modrinth.com/v2/search", "Modrinth搜索", filename, {"query": term, "limit": 20, "facets": '[ ["project_type:mod"] ]'})
            if response is None:
                continue
            completed = True
            try:
                hits = response.json().get("hits", [])
            except (ValueError, AttributeError):
                continue
            for hit in hits:
                slug = hit.get("slug", "")
                candidates.append(SearchCandidate("Modrinth", "https://modrinth.com/mod/" + slug, hit.get("title", ""), slug=slug, project_id=hit.get("project_id", ""), authors=[hit.get("author", "")], description=hit.get("description", "")))
        status = "已发现" if candidates else ("未找到" if completed else "请求失败")
        return SearchResult("Modrinth", status, self.deduplicate(candidates))

    def discover_bing(self, sources: Sequence[str], terms: Sequence[str], filename: str) -> Dict[str, SearchResult]:
        discovered = {}
        for source in unique(sources):
            candidates = []
            completed = False
            for term in terms:
                response = self.request("https://www.bing.com/search", "Bing搜索", filename, {"q": 'site:%s "%s"' % (SOURCE_SEARCH_SCOPES[source], term), "count": 10, "setlang": "zh-CN", "ensearch": 1})
                if response is None:
                    continue
                completed = True
                if etree is None:
                    continue
                for element in etree.HTML(response.text).xpath("//li[contains(@class,'b_algo')]//h2/a"):
                    url = element.get("href", "")
                    if is_allowed_url(url, source):
                        candidates.append(SearchCandidate(source, url, " ".join(element.xpath(".//text()")).strip(), slug=self.url_slug(url)))
            status = "已发现" if candidates else ("未找到" if completed else "请求失败")
            discovered[source] = SearchResult(source, status, self.deduplicate(candidates))
        return discovered

    @staticmethod
    def deduplicate(candidates: Sequence[SearchCandidate]) -> List[SearchCandidate]:
        unique_candidates = {}
        for candidate in candidates:
            key = (candidate.source, candidate.url.split("#", 1)[0].rstrip("/"), normalize_identifier(candidate.slug))
            unique_candidates.setdefault(key, candidate)
        return list(unique_candidates.values())

    def confirm_candidates(self, results: Dict[str, SearchResult], metadata: JarMetadata, filename: str, detail_executor=None) -> List[SearchCandidate]:
        all_candidates = []
        futures = []
        for source, result in results.items():
            for candidate in result.candidates:
                candidate.score = self.score_candidate(candidate, metadata, filename)
                if candidate.score <= 0:
                    continue
                if detail_executor is not None:
                    futures.append((candidate, detail_executor.submit(self.load_candidate_details, candidate, filename)))
                    continue
                try:
                    detailed = self.load_candidate_details(candidate, filename)
                except Exception:
                    detailed = None
                if detailed:
                    detailed.confirmed = True
                    all_candidates.append(detailed)
        for candidate, future in futures:
            try:
                detailed = future.result()
            except Exception:
                detailed = None
            if detailed:
                detailed.confirmed = True
                all_candidates.append(detailed)
        grouped = {}
        for candidate in all_candidates:
            key = normalize_identifier(candidate.project_id) or normalize_identifier(candidate.slug) or normalize_identifier(self.url_slug(candidate.url))
            if not key:
                key = candidate.url.split("#", 1)[0].rstrip("/")
            existing = grouped.get(key)
            if existing is None:
                grouped[key] = candidate
            else:
                existing.source_urls = unique(existing.source_urls + [candidate.url] + candidate.source_urls)
                if candidate.score > existing.score:
                    candidate.source_urls = unique(candidate.source_urls + [existing.url] + existing.source_urls)
                    grouped[key] = candidate
        result = list(grouped.values())
        result.sort(key=lambda candidate: candidate.score, reverse=True)
        return result

    def score_candidate(self, candidate: SearchCandidate, metadata: JarMetadata, filename: str) -> int:
        identifiers = [normalize_identifier(value) for value in metadata.mod_ids]
        names = [normalize_identifier(value) for value in metadata.names]
        file_terms = [normalize_identifier(value) for value in derive_search_terms(filename, JarMetadata())]
        values = [normalize_identifier(candidate.project_id), normalize_identifier(candidate.slug), normalize_identifier(candidate.name), normalize_identifier(self.url_slug(candidate.url))]
        score = 0
        if any(identifier and identifier == values[0] for identifier in identifiers): score = max(score, 100)
        if any(identifier and identifier == values[1] for identifier in identifiers): score = max(score, 90)
        if any(name and name == values[2] for name in names): score = max(score, 80)
        if any(term and term in values for term in file_terms): score = max(score, 70)
        if score == 0 and any(name and name in values[2] for name in names): score = 20
        if score == 0: return 0
        loader_name = (metadata.loader or "").casefold()
        if any(loader_name == str(value).casefold() for value in candidate.loaders) or loader_name in candidate.name.casefold(): score += 10
        if any(version in candidate.versions for version in metadata.versions): score += 10
        if re.search(r"resource-pack|modpack|plugin|shader", (candidate.name + " " + candidate.url).lower()): return 0
        return score

    def load_candidate_details(self, candidate: SearchCandidate, filename: str) -> Optional[SearchCandidate]:
        if candidate.source == "Modrinth":
            response = self.request("https://api.modrinth.com/v2/project/" + candidate.slug, "Modrinth详情", filename)
            if response is None:
                return None
            try:
                data = response.json()
            except (ValueError, AttributeError):
                return None
            candidate.name = data.get("title", candidate.name)
            candidate.description = data.get("description", candidate.description)
            candidate.client_side = data.get("client_side", "")
            candidate.server_side = data.get("server_side", "")
            candidate.loaders = data.get("loaders", [])
            candidate.versions = data.get("game_versions", [])
            return candidate
        response = self.request(candidate.url, candidate.source + "详情", filename)
        if response is None or etree is None:
            return None
        tree = etree.HTML(response.text)
        candidate.name = candidate.name or " ".join(tree.xpath("//title/text()")).strip()
        candidate.description = candidate.description or " ".join(tree.xpath("//meta[@name='description']/@content | //meta[@property='og:description']/@content"))
        if candidate.source == "MC百科":
            candidate.chinese_name = candidate.name.split("(")[0].strip()
            body = " ".join(tree.xpath("//body//text()"))
            for value in ("客户端需装", "客户端可选", "客户端无效"):
                if value in body: candidate.client_side = value; break
            for value in ("服务端需装", "服务端可选", "服务端无效"):
                if value in body: candidate.server_side = value; break
            candidate.versions = versions_from_text(body)
        return candidate

    def build_record(self, filename: str, metadata: JarMetadata, candidates: Sequence[SearchCandidate], results: Dict[str, SearchResult]) -> ModRecord:
        status = []
        for source in ("MC百科", "Modrinth", "Bing", "CurseForge"):
            result = results.get(source, SearchResult(source))
            matched = any(candidate.source == source for candidate in candidates)
            status.append(source + "=" + ("已匹配" if matched else result.status))
        if metadata.multiple: status.append("多 Mod JAR，需人工审核")
        if not candidates:
            source_results = [results.get(source, SearchResult(source)) for source in ("MC百科", "Modrinth", "CurseForge")]
            confidence = "请求失败" if all(result.failed for result in source_results) else "未找到"
            return ModRecord(filename=filename, metadata=metadata, confidence=confidence, status=status)
        best = candidates[0]
        close = len(candidates) > 1 and best.score - candidates[1].score <= 10
        confidence = "低，需人工审核" if metadata.multiple or close else ("高" if best.score >= 80 else "低，需人工审核")
        if len(candidates) > 1: status.append("候选数量=" + str(len(candidates)))
        return ModRecord(filename=filename, metadata=metadata, candidate=best, candidates=list(candidates), confidence=confidence, status=status)

    def read_jar_metadata(self, jar_path: str) -> JarMetadata:
        try:
            with zipfile.ZipFile(jar_path) as jar:
                names = set(jar.namelist())
                for file_name, loader in (("META-INF/neoforge.mods.toml", "NeoForge"), ("META-INF/mods.toml", "Forge")):
                    if file_name in names:
                        return self.parse_toml(jar.read(file_name).decode("utf-8", "ignore"), loader)
                for file_name, loader in (("fabric.mod.json", "Fabric"), ("quilt.mod.json", "Quilt")):
                    if file_name in names:
                        return self.parse_json(jar.read(file_name), loader)
        except (OSError, zipfile.BadZipFile) as error:
            return JarMetadata(error=type(error).__name__)
        return JarMetadata()

    @staticmethod
    def parse_toml(content: str, loader: str) -> JarMetadata:
        blocks = re.split(r"\[\[mods\]\]", content)[1:]
        mod_ids, names = [], []
        for block in blocks:
            mod_id = re.search(r"^\s*modId\s*=\s*[\"']([^\"']+)", block, re.M)
            display_name = re.search(r"^\s*displayName\s*=\s*[\"']([^\"']+)", block, re.M)
            if mod_id: mod_ids.append(mod_id.group(1))
            if display_name: names.append(display_name.group(1))
        authors = []
        for raw_authors in re.findall(r"^\s*authors?\s*=\s*(.+)$", content, re.M):
            authors.extend(re.findall(r"[\"']([^\"']+)[\"']", raw_authors))
        description = re.search(r"^\s*description\s*=\s*[\"']([^\"']+)", content, re.M)
        versions = toml_minecraft_versions(content)
        return JarMetadata(unique(mod_ids), unique(names), unique(authors), description.group(1) if description else "", versions, loader)

    @staticmethod
    def parse_json(raw: bytes, loader: str) -> JarMetadata:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JarMetadata(loader=loader, error="JSON 解析失败")
        if loader == "Quilt":
            data = data.get("quilt_loader", {})
            metadata = data.get("metadata", {})
            entry = {**data, **metadata}
        else:
            entry = data
        authors = entry.get("authors", [])
        authors = [item.get("name", "") if isinstance(item, dict) else str(item) for item in (authors if isinstance(authors, list) else [authors])]
        versions = json_minecraft_versions(data, loader)
        return JarMetadata([entry.get("id", "")], [entry.get("name", "")], unique(authors), entry.get("description", ""), unique(versions), loader)

    def wait_for_rate_limit(self):
        """兼容旧调用的全局每秒限流；新请求走按域名限流器。"""
        if not hasattr(self, "_request_lock"):
            self._request_lock = threading.Lock()
        with self._request_lock:
            interval, now = 1 / MAX_REQUESTS_PER_SECOND, time.monotonic()
            if self._last_request_time is not None and interval - (now - self._last_request_time) > 0:
                time.sleep(interval - (now - self._last_request_time))
            self._last_request_time = time.monotonic()

    def _limiter_for_url(self, url: str) -> Optional[WindowRateLimiter]:
        if not hasattr(self, "_limiters"):
            return None
        host = source_url_host(url)
        if host == "www.bing.com":
            host = "bing.com"
        domain = next((value for value in RATE_LIMITS if host == value or host.endswith("." + value)), None)
        if domain is None:
            return None
        if not hasattr(self, "_limiters_lock"):
            self._limiters_lock = threading.Lock()
        with self._limiters_lock:
            limiter = self._limiters.get(domain)
            if limiter is None:
                config = RATE_LIMITS[domain]
                limiter = self._limiters[domain] = WindowRateLimiter(config["per_second"], config["per_minute"])
            return limiter

    def _wait_for_request_limit(self, url: str):
        limiter = self._limiter_for_url(url)
        if limiter is None:
            self.wait_for_rate_limit()
        else:
            limiter.acquire()

    def request(self, url: str, stage: str, filename: str, params=None):
        for attempt in range(MAX_RETRIES + 1):
            self._wait_for_request_limit(url)
            try:
                response = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
                if response.status_code in RETRYABLE_STATUS_CODES and attempt < MAX_RETRIES:
                    self.log_retry(stage, filename, attempt + 1, "HTTP " + str(response.status_code))
                    time.sleep(RETRY_BACKOFF_SECONDS * (2 ** attempt)); continue
                if response.status_code in RETRYABLE_STATUS_CODES:
                    self.log_failure(stage, filename, "HTTP " + str(response.status_code))
                    return None
                response.raise_for_status()
                return response
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as error:
                if attempt < MAX_RETRIES:
                    self.log_retry(stage, filename, attempt + 1, type(error).__name__)
                    time.sleep(RETRY_BACKOFF_SECONDS * (2 ** attempt)); continue
                self.log_failure(stage, filename, type(error).__name__)
                return None
            except requests.exceptions.RequestException as error:
                self.log_failure(stage, filename, type(error).__name__)
                return None
        return None

    @staticmethod
    def log_retry(stage: str, filename: str, retry_number: int, reason: str):
        print(f"[网络重试] {filename} {stage}: 第 {retry_number} 次重试，原因: {reason}")

    @staticmethod
    def log_failure(stage: str, filename: str, reason: str):
        print(f"[网络失败] {filename} {stage}: {reason}")

    @staticmethod
    def url_slug(url: str) -> str:
        parts = [part for part in url.split("?")[0].rstrip("/").split("/") if part]
        return parts[-1] if parts else ""


if __name__ == "__main__":
    Manager("./mods")
