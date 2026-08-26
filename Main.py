from lxml import etree
from os import path
import xlwings as xw
import requests
import re
import os
import zipfile
import time
from typing import Optional


MAX_RETRIES = 3
REQUEST_TIMEOUT = (5, 15)
MAX_REQUESTS_PER_SECOND = 50
RETRY_BACKOFF_SECONDS = 1
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/109.0.0.0 Safari/537.36 Edg/109.0.1518.70"
)


class Manager:
    def __init__(self, modFilePath):
        self.filename2Simple = {}
        self.filename2Real = {}
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self._last_request_time = None

        try:
            self._analyze_mods(modFilePath)
        finally:
            self.session.close()

    def _analyze_mods(self, modFilePath):
        files = os.listdir(modFilePath)
        num = 1
        xb = xw.Book()
        xs = xb.sheets["Sheet1"]
        xs.range(num, 2).value = "Mod文件名"
        xs.range(num, 3).value = "Mod信息页名"
        xs.range(num, 4).value = "Mod信息"
        xs.range(num, 5).value = "Mod搜索名"
        xs.range(num, 6).value = "是否为Forge Mod"

        for file in files:
            num += 1
            fileName = path.basename(file)
            isForgeMod = self.isForgeMod(fileName)
            modName = self.getModName(fileName)
            if not modName:
                modName = self.simplifyName(fileName)
            self.filename2Simple[fileName] = modName

            searchSucceeded = self.loadSearchWeb(modName, fileName)
            if not searchSucceeded:
                self._write_result_row(xs, num, num - 1, fileName, "", "", modName, isForgeMod)
                print(fileName, "->", modName, ": 网络请求失败，已跳过")
                continue

            searchResultDic = self.getModname2UrlDic()
            if not searchResultDic:
                self._write_result_row(xs, num, num - 1, fileName, "查无此mod", "\\", modName, isForgeMod)
                print(fileName, "->", modName, ": 查无此mod")
                continue

            modInfoName = list(searchResultDic.keys())[0]
            modInfoSide = self.isServerNeeded(list(searchResultDic.values())[0], fileName)
            if modInfoSide is None:
                self._write_result_row(xs, num, num - 1, fileName, modInfoName, "", modName, isForgeMod)
                print(fileName, "->", modName, "->", modInfoName, ": 网络请求失败，已跳过")
                continue

            self._write_result_row(xs, num, num - 1, fileName, modInfoName, modInfoSide, modName, isForgeMod)
            print(fileName, "->", modName, "->", modInfoName, ":", modInfoSide)
        xb.save("result.xlsx")

    @staticmethod
    def _write_result_row(sheet, row, index, file_name, info_name, info, search_name, is_forge_mod):
        sheet.range(row, 1).value = index
        sheet.range(row, 2).value = file_name
        sheet.range(row, 3).value = info_name
        sheet.range(row, 4).value = info
        sheet.range(row, 5).value = search_name
        sheet.range(row, 6).value = is_forge_mod

    def _wait_for_rate_limit(self):
        interval = 1 / MAX_REQUESTS_PER_SECOND
        now = time.monotonic()
        if self._last_request_time is not None:
            wait_time = interval - (now - self._last_request_time)
            if wait_time > 0:
                time.sleep(wait_time)
        self._last_request_time = time.monotonic()

    def _request(self, url, stage, file_name, params=None) -> Optional[requests.Response]:
        for attempt in range(MAX_RETRIES + 1):
            self._wait_for_rate_limit()
            try:
                response = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
                if response.status_code in RETRYABLE_STATUS_CODES:
                    if attempt < MAX_RETRIES:
                        self._log_retry(stage, file_name, attempt + 1, response.status_code)
                        time.sleep(RETRY_BACKOFF_SECONDS * (2 ** attempt))
                        continue
                    self._log_failure(stage, file_name, "HTTP " + str(response.status_code))
                    return None
                response.raise_for_status()
                return response
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as error:
                if attempt < MAX_RETRIES:
                    self._log_retry(stage, file_name, attempt + 1, type(error).__name__)
                    time.sleep(RETRY_BACKOFF_SECONDS * (2 ** attempt))
                    continue
                self._log_failure(stage, file_name, type(error).__name__)
                return None
            except requests.exceptions.HTTPError as error:
                status_code = getattr(error.response, "status_code", "unknown")
                self._log_failure(stage, file_name, "HTTP " + str(status_code))
                return None
            except requests.exceptions.RequestException as error:
                self._log_failure(stage, file_name, type(error).__name__)
                return None
        return None

    @staticmethod
    def _log_retry(stage, file_name, retry_number, reason):
        print(f"[网络重试] {file_name} {stage}: 第 {retry_number} 次重试，原因: {reason}")

    @staticmethod
    def _log_failure(stage, file_name, reason):
        print(f"[网络失败] {file_name} {stage}: {reason}")

    def getModName(self, modFileName):
        if re.search(r"\.jar$", modFileName):  # Check if is jar file
            with zipfile.ZipFile("./mods/" + modFileName, 'r') as jarfile:
                infiles = jarfile.namelist()
                for infile in infiles:
                    if re.search(r"mods.toml$", infile):
                        content = jarfile.read(infile).decode("UTF-8", errors="ignore")
                        modName = re.search(r"displayName=\"(.+)\"", content)
                        if modName:
                            return modName.group(1)
        return None

    def simplifyName(self, name):
        finder = re.compile(r"^([a-zA-Z'_]*)?(【(.*)】)?([a-zA-Z'_]*)")
        findResult = finder.search(name)
        if findResult.group(3) is None:
            return findResult.group(1)
        else:
            if findResult.group(3) == "前置":
                return findResult.group(4)
            else:
                return findResult.group(4)

    def loadSearchWeb(self, modName, fileName="未知文件"):
        params = {
            "key": modName,
            "filter": 1,
        }
        response = self._request("https://search.mcmod.cn/s", "搜索", fileName, params=params)
        if response is None:
            return False
        with open("searchWeb.html", "w", encoding="UTF-8") as f:
            f.write(response.text)
        return True

    def getModname2UrlDic(self, searchWebSrc="searchWeb.html"):
        parser = etree.HTMLParser(recover=True, encoding="UTF-8")
        tree = etree.parse(searchWebSrc, parser=parser)
        elements = tree.xpath("//div[@class='result-item']/div[@class='head']/a[@target='_blank']")
        # for element in elements:
        #     # d = etree.tostring(element, encoding="UTF-8").decode("UTF-8")
        #
        #     # print(element.get("href"))
        #     # print('*' * 50)
        # for element in elements:
        # print(' '.join(etree.tostring(element, method="text", encoding="UTF-8").decode("UTF-8").split()))
        # print('*' * 50)
        # print(etree.tostring(tree, encoding="UTF-8").decode("UTF-8"))
        modname2UrlDic = {}
        for element in elements:
            modname2UrlDic[
                ' '.join(
                    etree.tostring(element, method="text", encoding="UTF-8").decode("UTF-8").split())] = element.get(
                "href")
        # print(modname2UrlDic)
        # print('-' * 50)
        return modname2UrlDic

    def isServerNeeded(self, modUrl, fileName="未知文件"):
        response = self._request(modUrl, "详情", fileName)
        if response is None:
            return None
        # Store the mod web file
        with open("modWeb.html", "w", encoding="UTF-8") as f:
            f.write(response.text)
        # Parse the mod web file
        parser = etree.HTMLParser(recover=True, encoding="UTF-8")
        tree = etree.parse("modWeb.html", parser=parser)
        elements = tree.xpath("//div[@class='class-info']//ul[@class='col-lg-12']/li[@class='col-lg-4']")

        for element in elements:
            str = etree.tostring(element, encoding="UTF-8", method="text").decode("UTF-8")
            if re.search("服务端需装", str):
                return "服务端需装"
            if re.search("服务端无效", str):
                return "服务端无效"
            if re.search("服务端可选", str):
                return "服务端可选"
        return "未知" + '[' + modUrl + ']'

    def isForgeMod(self, fileName):
        with zipfile.ZipFile("./mods/" + fileName, 'r') as jarfile:
            infiles = jarfile.namelist()
            for infile in infiles:
                if re.search(r"mods.toml$", infile):
                    return True
        return False

if __name__ == '__main__':
    Manager("./mods")
