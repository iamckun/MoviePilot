import json
import shutil
import traceback
from pathlib import Path
from typing import Dict, Tuple, Optional, List

from cachetools import TTLCache, cached

from app.core.config import settings
from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger
from app.schemas.types import SystemConfigKey
from app.utils.http import RequestUtils
from app.utils.singleton import Singleton
from app.utils.system import SystemUtils


class PluginHelper(metaclass=Singleton):
    """
    插件市场管理，下载安装插件到本地
    """

    _base_url = f"{settings.GITHUB_PROXY}https://raw.githubusercontent.com/%s/%s/main/"

    _install_reg = f"{settings.MP_SERVER_HOST}/plugin/install/%s"

    _install_report = f"{settings.MP_SERVER_HOST}/plugin/install"

    _install_statistic = f"{settings.MP_SERVER_HOST}/plugin/statistic"

    def __init__(self):
        self.systemconfig = SystemConfigOper()
        if settings.PLUGIN_STATISTIC_SHARE:
            if not self.systemconfig.get(SystemConfigKey.PluginInstallReport):
                if self.install_report():
                    self.systemconfig.set(SystemConfigKey.PluginInstallReport, "1")

    @property
    def proxies(self):
        return None if settings.GITHUB_PROXY else settings.PROXY

    @cached(cache=TTLCache(maxsize=1000, ttl=1800))
    def get_plugins(self, repo_url: str) -> Dict[str, dict]:
        """
        获取Github所有最新插件列表
        :param repo_url: Github仓库地址
        """
        if not repo_url:
            return {}
        user, repo = self.get_repo_info(repo_url)
        if not user or not repo:
            return {}
        raw_url = self._base_url % (user, repo)
        res = RequestUtils(proxies=self.proxies, headers=settings.GITHUB_HEADERS,
                           timeout=10).get_res(f"{raw_url}package.json")
        if res:
            try:
                return json.loads(res.text)
            except json.JSONDecodeError:
                logger.error(f"插件包数据解析失败：{res.text}")
                return {}
        return {}

    @staticmethod
    def get_repo_info(repo_url: str) -> Tuple[Optional[str], Optional[str]]:
        """
        获取Github仓库信息
        :param repo_url: Github仓库地址
        """
        if not repo_url:
            return None, None
        if not repo_url.endswith("/"):
            repo_url += "/"
        if repo_url.count("/") < 6:
            repo_url = f"{repo_url}main/"
        try:
            user, repo = repo_url.split("/")[-4:-2]
        except Exception as e:
            logger.error(f"解析Github仓库地址失败：{str(e)} - {traceback.format_exc()}")
            return None, None
        return user, repo

    @cached(cache=TTLCache(maxsize=1, ttl=1800))
    def get_statistic(self) -> Dict:
        """
        获取插件安装统计
        """
        if not settings.PLUGIN_STATISTIC_SHARE:
            return {}
        res = RequestUtils(timeout=10).get_res(self._install_statistic)
        if res and res.status_code == 200:
            return res.json()
        return {}

    def install_reg(self, pid: str) -> bool:
        """
        安装插件统计
        """
        if not settings.PLUGIN_STATISTIC_SHARE:
            return False
        if not pid:
            return False
        res = RequestUtils(timeout=5).get_res(self._install_reg % pid)
        if res and res.status_code == 200:
            return True
        return False

    def install_report(self) -> bool:
        """
        上报存量插件安装统计
        """
        if not settings.PLUGIN_STATISTIC_SHARE:
            return False
        plugins = self.systemconfig.get(SystemConfigKey.UserInstalledPlugins)
        if not plugins:
            return False
        res = RequestUtils(content_type="application/json",
                           timeout=5).post(self._install_report,
                                           json={
                                               "plugins": [
                                                   {
                                                       "plugin_id": plugin,
                                                   } for plugin in plugins
                                               ]
                                           })
        return True if res else False

    def install(self, pid: str, repo_url: str) -> Tuple[bool, str]:
        """
        安装插件
        """
        if SystemUtils.is_frozen():
            return False, "可执行文件模式下，只能安装本地插件"

        # 从Github的repo_url获取用户和项目名
        user, repo = self.get_repo_info(repo_url)
        if not user or not repo:
            return False, "不支持的插件仓库地址格式"

        def __get_filelist(_p: str) -> Tuple[Optional[list], Optional[str]]:
            """
            获取插件的文件列表
            """
            file_api = f"https://api.github.com/repos/{user}/{repo}/contents/plugins/{_p}"
            r = RequestUtils(proxies=settings.PROXY, headers=settings.GITHUB_HEADERS, timeout=30).get_res(file_api)
            if r is None:
                return None, "连接仓库失败"
            elif r.status_code != 200:
                return None, f"连接仓库失败：{r.status_code} - " \
                             f"{'超出速率限制，请配置GITHUB_TOKEN环境变量或稍后重试' if r.status_code == 403 else r.reason}"
            ret = r.json()
            if ret and ret[0].get("message") == "Not Found":
                return None, "插件在仓库中不存在"
            return ret, ""

        def __download_files(_p: str, _l: List[dict]) -> Tuple[bool, str]:
            """
            下载插件文件
            """
            if not _l:
                return False, "文件列表为空"
            for item in _l:
                if item.get("download_url"):
                    download_url = f"{settings.GITHUB_PROXY}{item.get('download_url')}"
                    # 下载插件文件
                    res = RequestUtils(proxies=self.proxies,
                                       headers=settings.GITHUB_HEADERS, timeout=60).get_res(download_url)
                    if not res:
                        return False, f"文件 {item.get('name')} 下载失败！"
                    elif res.status_code != 200:
                        return False, f"下载文件 {item.get('name')} 失败：{res.status_code} - " \
                                      f"{'超出速率限制，请配置GITHUB_TOKEN环境变量或稍后重试' if res.status_code == 403 else res.reason}"
                    # 创建插件文件夹
                    file_path = Path(settings.ROOT_PATH) / "app" / item.get("path")
                    if not file_path.parent.exists():
                        file_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(res.text)
                else:
                    # 递归下载子目录
                    p = f"{_p}/{item.get('name')}"
                    l, m = __get_filelist(p)
                    if not l:
                        return False, m
                    __download_files(p, l)
            return True, ""

        if not pid or not repo_url:
            return False, "参数错误"

        # 获取插件的文件列表
        """
        [
            {
                "name": "__init__.py",
                "path": "plugins/autobackup/__init__.py",
                "sha": "cd10eba3f0355d61adeb35561cb26a0a36c15a6c",
                "size": 12385,
                "url": "https://api.github.com/repos/jxxghp/MoviePilot-Plugins/contents/plugins/autobackup/__init__.py?ref=main",
                "html_url": "https://github.com/jxxghp/MoviePilot-Plugins/blob/main/plugins/autobackup/__init__.py",
                "git_url": "https://api.github.com/repos/jxxghp/MoviePilot-Plugins/git/blobs/cd10eba3f0355d61adeb35561cb26a0a36c15a6c",
                "download_url": "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/plugins/autobackup/__init__.py",
                "type": "file",
                "_links": {
                    "self": "https://api.github.com/repos/jxxghp/MoviePilot-Plugins/contents/plugins/autobackup/__init__.py?ref=main",
                    "git": "https://api.github.com/repos/jxxghp/MoviePilot-Plugins/git/blobs/cd10eba3f0355d61adeb35561cb26a0a36c15a6c",
                    "html": "https://github.com/jxxghp/MoviePilot-Plugins/blob/main/plugins/autobackup/__init__.py"
                }
            }
        ]
        """
        # 获取第一级文件列表
        file_list, msg = __get_filelist(pid.lower())
        if not file_list:
            return False, msg
        # 本地存在时先删除
        plugin_dir = Path(settings.ROOT_PATH) / "app" / "plugins" / pid.lower()
        if plugin_dir.exists():
            shutil.rmtree(plugin_dir, ignore_errors=True)
        # 下载所有文件
        __download_files(pid.lower(), file_list)
        # 插件目录下如有requirements.txt则安装依赖
        requirements_file = plugin_dir / "requirements.txt"
        if requirements_file.exists():
            # 初始化
            protocol = host = port = username = password = PROXY_CHAINS = PIP_PROXY = ""
            # 返回json格式解析结果
            parsed_url = settings.PROXY_URLPARSE()
            if parsed_url:
                protocol = parsed_url.get("scheme", "").lower()
                username = parsed_url.get("username", "")
                password = parsed_url.get("password", "")
                host = parsed_url.get("host", "").lower()
                port = parsed_url.get("port", "")

            # 全局优先，镜像站不存在时，使用全局代理
            if settings.PROXY_SUPPLEMENT:

                # 检查settings.PROXY_HOST的协议类型，http或https
                if protocol in {"http", "https"}:
                    if settings.PIP_PROXY:
                        PIP_PROXY = f" -i {settings.PIP_PROXY} " if settings.PIP_PROXY else ""
                    else:
                        # 有主机名与端口号的时候
                        if host and port:
                            PIP_PROXY = f" --proxy={settings.PROXY_HOST} " if settings.PROXY_HOST else ""

                # Todo:目前proxychains4的临时调用命令不支持socks5h和socks4a，需要生成临时配置文件才能解决，后面考虑支持一下
                elif protocol in {"socks4", "socks4a", "socks5", "socks5h"}:
                    # 没有主机名，端口号
                    if not host or not port:
                        PIP_PROXY = f" -i {settings.PIP_PROXY} " if settings.PIP_PROXY else ""
                    # 将拓展的socks协议转换为proxychains4支持的socks4和socks5
                    else:
                        if protocol in {"socks5", "socks5h"}:
                            protocol = "socks5"
                        elif protocol in {"socks4", "socks4a"}:
                            protocol = "socks4"
                        # 生成配置
                        PROXY_CHAINS = f"proxychains4 -f <( echo -e '[ProxyList]\n{protocol} {host} {port} {username} {password}')"

                # 不支持的协议类型
                else:
                    PIP_PROXY = f" -i {settings.PIP_PROXY} " if settings.PIP_PROXY else ""

            # 本地优先，镜像站不存在时，不使用全局代理
            else:
                PIP_PROXY = f" -i {settings.PIP_PROXY} " if settings.PIP_PROXY else ""

            SystemUtils.execute(f"{PROXY_CHAINS} pip install -r {requirements_file} {PIP_PROXY}  > /dev/null 2>&1")
        # 安装成功后统计
        self.install_reg(pid)

        return True, ""
