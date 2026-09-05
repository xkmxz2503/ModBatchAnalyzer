import json
import os
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import MagicMock, patch

from Main import (
    OUTPUT_HEADERS,
    JarMetadata,
    Manager,
    SearchCandidate,
    SearchResult,
    derive_search_terms,
    map_side,
    normalize_identifier,
)


def make_manager():
    manager = Manager.__new__(Manager)
    manager._request_lock = __import__("threading").Lock()
    manager._last_request_time = None
    return manager


class MetadataTests(unittest.TestCase):
    def make_jar(self, entries):
        handle = tempfile.NamedTemporaryFile(suffix=".jar", delete=False)
        handle.close()
        with zipfile.ZipFile(handle.name, "w") as archive:
            for name, content in entries.items():
                archive.writestr(name, content)
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_all_metadata_formats_and_dependency_versions(self):
        item = make_manager()
        neo = self.make_jar({"META-INF/neoforge.mods.toml": "[[mods]]\nmodId=\"neo\"\ndisplayName=\"Neo\"\nversion=\"9.9.9\"\n[[dependencies.neo]]\nmodId=\"minecraft\"\nversionRange=\"[1.21.1,1.22)\""})
        forge = self.make_jar({"META-INF/mods.toml": "[[mods]]\nmodId=\"one\"\n[[mods]]\nmodId=\"two\"\nversion=\"9.9.9\"\n[[dependencies.one]]\nmodId=\"minecraft\"\nversionRange=\"[1.20.1,1.21)\""})
        fabric = self.make_jar({"fabric.mod.json": json.dumps({"id": "fabric_id", "name": "Fabric", "depends": {"minecraft": ">=1.20.4"}})})
        quilt = self.make_jar({"quilt.mod.json": json.dumps({"quilt_loader": {"metadata": {"id": "quilt_id", "name": "Quilt"}, "depends": {"minecraft": ["1.21.1"]}}})})
        neo_metadata = item.read_jar_metadata(neo)
        forge_metadata = item.read_jar_metadata(forge)
        self.assertEqual(neo_metadata.loader, "NeoForge")
        self.assertEqual(neo_metadata.versions, ["[1.21.1,1.22)"])
        self.assertTrue(forge_metadata.multiple)
        self.assertEqual(forge_metadata.versions, ["[1.20.1,1.21)"])
        self.assertEqual(item.read_jar_metadata(fabric).versions, [">=1.20.4"])
        self.assertEqual(item.read_jar_metadata(quilt).loader, "Quilt")

    def test_invalid_jar_is_recorded(self):
        bad = tempfile.NamedTemporaryFile(suffix=".jar", delete=False)
        bad.write(b"bad")
        bad.close()
        self.addCleanup(os.unlink, bad.name)
        self.assertEqual(make_manager().read_jar_metadata(bad.name).error, "BadZipFile")

    def test_filename_and_mod_version_are_not_minecraft_versions(self):
        handle = tempfile.NamedTemporaryFile(prefix="mod-1.21.1-fabric", suffix=".jar", delete=False)
        handle.close()
        with zipfile.ZipFile(handle.name, "w") as archive:
            archive.writestr("fabric.mod.json", json.dumps({"id": "example", "version": "9.9.9"}))
        self.addCleanup(lambda: os.path.exists(handle.name) and os.unlink(handle.name))
        self.assertEqual(make_manager().read_jar_metadata(handle.name).versions, [])


class PureLogicTests(unittest.TestCase):
    def test_normalization_terms_and_side_mapping(self):
        self.assertEqual(normalize_identifier(" My_Mod-Name "), "mymodname")
        self.assertIn("example-mod", derive_search_terms("[test]example-mod-1.21.1.jar", JarMetadata()))
        self.assertEqual(map_side("required", "客户端"), "客户端需装")
        self.assertEqual(map_side("other", "服务端"), "未知")

    def test_ambiguous_candidates_require_review(self):
        item = make_manager()
        metadata = JarMetadata(mod_ids=["example"], names=["Example Mod"], loader="Fabric", versions=["1.21.1"])
        candidate = SearchCandidate("Modrinth", "https://modrinth.com/mod/example", "Example Mod", slug="example", project_id="example", loaders=["fabric"], versions=["1.21.1"])
        self.assertGreaterEqual(item.score_candidate(candidate, metadata, "example-1.21.1.jar"), 120)
        pack = SearchCandidate("Modrinth", "https://modrinth.com/mod/example-pack", "Example Modpack", slug="example-pack")
        self.assertEqual(item.score_candidate(pack, metadata, "example.jar"), 0)

    def test_output_shape_and_mcmod_side_priority(self):
        item = make_manager()
        metadata = JarMetadata(mod_ids=["x"], names=["X"], authors=["A"], description="D", loader="Fabric")
        candidate = SearchCandidate("Modrinth", "https://modrinth.com/mod/x", "X", client_side="required", server_side="optional")
        record = item.build_record("x.jar", metadata, [candidate], {"MC百科": SearchResult("MC百科"), "Modrinth": SearchResult("Modrinth", "已发现", [candidate]), "Bing": SearchResult("Bing"), "CurseForge": SearchResult("CurseForge")})
        values = item.record_values(1, record)
        self.assertEqual(len(OUTPUT_HEADERS), 14)
        self.assertEqual(len(values), 14)
        self.assertEqual(values[8], "客户端需装")

    def test_sheet_formatting_uses_explicit_dimensions(self):
        sheet = MagicMock()
        used = MagicMock()
        sheet.range.return_value.resize.return_value = used
        data_range = MagicMock()
        data_range.resize.return_value.value = ["1", "长文件名" * 20] + [""] * 12
        sheet.range.side_effect = lambda *args: sheet.range.return_value if args == (1, 1) else data_range
        Manager.format_sheet(sheet, 2)
        self.assertTrue(used.api.WrapText)
        self.assertEqual(data_range.column_width, 36)
        self.assertGreater(data_range.row_height, 20)

    def test_record_log_contains_local_and_result_status(self):
        item = make_manager()
        metadata = JarMetadata(mod_ids=["x"], loader="Fabric", versions=[">=1.21"])
        record = item.build_record("x.jar", metadata, [], {source: SearchResult(source) for source in ("MC百科", "Modrinth", "Bing", "CurseForge")})
        output = StringIO()
        with redirect_stdout(output):
            item.log_record(record)
        self.assertIn("[结果] x.jar", output.getvalue())
        self.assertIn("未找到可信候选", output.getvalue())


class PipelineTests(unittest.TestCase):
    def test_directory_logs_progress_and_skips_non_jar(self):
        item = make_manager()
        metadata = JarMetadata(mod_ids=["known"], loader="Fabric")
        record = item.build_record("known.jar", metadata, [], {source: SearchResult(source) for source in ("MC百科", "Modrinth", "Bing", "CurseForge")})
        with tempfile.TemporaryDirectory() as directory:
            jar_path = os.path.join(directory, "known.jar")
            with zipfile.ZipFile(jar_path, "w") as archive:
                archive.writestr("fabric.mod.json", json.dumps({"id": "known"}))
            with open(os.path.join(directory, "damaged.jar"), "wb") as handle:
                handle.write(b"not a zip archive")
            with open(os.path.join(directory, "notes.txt"), "w", encoding="utf-8") as handle:
                handle.write("ignored")
            workbook = MagicMock()
            workbook.sheets.__getitem__.return_value = MagicMock()
            with patch("Main.xw") as excel, patch.object(item, "analyze_jar", return_value=record):
                excel.Book.return_value = workbook
                output = StringIO()
                with redirect_stdout(output):
                    item.analyze_directory(directory)
        text = output.getvalue()
        self.assertIn("[开始] 发现 2 个 JAR", text)
        self.assertIn("[跳过] notes.txt", text)
        self.assertIn("[元数据异常] damaged.jar: BadZipFile", text)
        self.assertIn("[结果] known.jar", text)
        self.assertIn("[完成] 已处理 2 个 JAR", text)

    def test_bing_fills_missing_source_and_always_checks_curseforge(self):
        item = make_manager()
        metadata = JarMetadata(mod_ids=["known"], names=["Known"])
        mc = SearchResult("MC百科", "已发现", [SearchCandidate("MC百科", "https://mcmod.cn/known", "Known", slug="known")])
        mr = SearchResult("Modrinth", "未找到")
        discovered = {"Modrinth": SearchResult("Modrinth", "未找到"), "CurseForge": SearchResult("CurseForge", "未找到")}
        with patch.object(item, "search_mcmod", return_value=mc), patch.object(item, "search_modrinth", return_value=mr), patch.object(item, "discover_bing", return_value=discovered) as bing, patch.object(item, "load_candidate_details", side_effect=lambda candidate, filename: candidate):
            item.analyze_jar("known.jar", metadata)
        self.assertEqual(bing.call_args.args[0], ["Modrinth", "CurseForge"])

    def test_all_sources_failed_isolated(self):
        item = make_manager()
        results = {source: SearchResult(source, "请求失败") for source in ("MC百科", "Modrinth", "Bing", "CurseForge")}
        record = item.build_record("broken.jar", JarMetadata(), [], results)
        self.assertEqual(record.confidence, "请求失败")

    def test_one_direct_source_failure_does_not_stop_other_source(self):
        item = make_manager()
        metadata = JarMetadata(mod_ids=["known"])
        modrinth = SearchResult("Modrinth", "已发现", [SearchCandidate("Modrinth", "https://modrinth.com/mod/known", "Known", slug="known")])
        with patch.object(item, "search_mcmod", side_effect=RuntimeError), patch.object(item, "search_modrinth", return_value=modrinth), patch.object(item, "discover_bing", return_value={"MC百科": SearchResult("MC百科", "请求失败"), "CurseForge": SearchResult("CurseForge", "未找到")}), patch.object(item, "load_candidate_details", side_effect=lambda candidate, filename: candidate):
            record = item.analyze_jar("known.jar", metadata)
        self.assertEqual(record.candidate.source, "Modrinth")
        self.assertIn("MC百科=请求失败", record.status)


if __name__ == "__main__":
    unittest.main()
