import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """从 .env 载入键值对，不覆盖已存在的环境变量（与 proxy.py 同优先级）。"""
    if not path.is_file():
        return
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(path, override=False)
        return
    except ImportError:
        pass
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if key:
            os.environ.setdefault(key, value.strip().strip("'\""))


_load_dotenv(_BASE_DIR / ".env")


def _env(name: str, default: str) -> str:
    """读取环境变量，空值回退到默认值。"""
    return os.getenv(name) or default


HOST = _env("HOST", "127.0.0.1")
PORT = _env("PORT", "9099")
DEFAULT_PROXY_URL = f"http://{HOST}:{PORT}"

# 官方原始端点
OFFICIAL_ENDPOINTS = {
    "Zis": "https://autopush-cloudcode-pa.sandbox.googleapis.com",
    "Dis": "https://preprod-daily-cloudcode-pa.sandbox.googleapis.com",
    "Uis": "https://daily-cloudcode-pa.googleapis.com",
    "Gis": "https://cloudcode-pa.googleapis.com",
}

# 匹配任意本地代理地址（含 0.0.0.0 / 局域网 IP / 自定义端口）
_LOCAL_PROXY_PATTERN = r"http://(?:\d{1,3}\.){3}\d{1,3}:\d+"


def kill_language_server():
    """强制清理脱壳常驻的后台守护进程"""
    try:
        res = subprocess.run(
            ["taskkill", "/F", "/IM", "language_server_windows_x64.exe"],
            capture_output=True,
            text=True,
        )
        if "SUCCESS" in res.stdout or "成功" in res.stdout:
            print("[+] 已成功终止后台残留的 language_server_windows_x64.exe 进程")
        else:
            print("[*] 后台无运行中的 language_server 守护进程")
    except Exception as e:
        print(f"[-] 进程清理异常: {e}")


def locate_main_js(input_path: str) -> str:
    """智能解析并定位 main.js 的绝对路径"""
    cleaned = input_path.strip().strip('"').strip("'")

    # 候选路径检测
    candidates = [
        cleaned,
        os.path.join(cleaned, "resources", "app", "out", "main.js"),
        os.path.join(cleaned, "app", "out", "main.js"),
        os.path.join(cleaned, "out", "main.js"),
        os.path.join(cleaned, "main.js"),
    ]

    for candidate in candidates:
        if os.path.isfile(candidate) and os.path.basename(candidate).lower() == "main.js":
            return os.path.abspath(candidate)

    return ""


def patch_file(main_js_path: str, proxy_url: str):
    """应用反代端点补丁"""
    bak_path = main_js_path + ".bak"

    # 首次修改自动保留原始副本
    if not os.path.exists(bak_path):
        shutil.copy2(main_js_path, bak_path)
        print(f"[+] 已创建原始备份文件: {bak_path}")

    with open(main_js_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 兼容压缩文件中的等号无空格、单空格以及重复打补丁的情况
    patch_rules = [
        (r'(Zis\s*=\s*)"(?:https://autopush-cloudcode-pa\.sandbox\.googleapis\.com|' + _LOCAL_PROXY_PATTERN + r')"', f'\\1"{proxy_url}"'),
        (r'(Dis\s*=\s*)"(?:https://preprod-daily-cloudcode-pa\.sandbox\.googleapis\.com|' + _LOCAL_PROXY_PATTERN + r')"', f'\\1"{proxy_url}"'),
        (r'(Uis\s*=\s*)"(?:https://daily-cloudcode-pa\.googleapis\.com|' + _LOCAL_PROXY_PATTERN + r')"', f'\\1"{proxy_url}"'),
        (r'(Gis\s*=\s*)"(?:https://cloudcode-pa\.googleapis\.com|' + _LOCAL_PROXY_PATTERN + r')"', f'\\1"{proxy_url}"'),
    ]

    modified_content = content
    total_replaced = 0

    for pattern, repl in patch_rules:
        modified_content, count = re.subn(pattern, repl, modified_content)
        total_replaced += count

    if total_replaced == 0:
        print("[-] 未匹配到需要替换的端点变量，可能已经处于该状态或文件结构已被改动。")
        return

    with open(main_js_path, "w", encoding="utf-8") as f:
        f.write(modified_content)

    print(f"[√] 补丁注入成功！共替换 {total_replaced} 处端点变量为: {proxy_url}")
    kill_language_server()


def restore_file(main_js_path: str):
    """恢复官方端点"""
    bak_path = main_js_path + ".bak"

    # 若存在 .bak 备份，优先使用干净备份覆盖
    if os.path.exists(bak_path):
        shutil.copy2(bak_path, main_js_path)
        print(f"[√] 已直接从备份文件恢复: {bak_path}")
        kill_language_server()
        return

    # 无备份时进行正则逆向替换
    with open(main_js_path, "r", encoding="utf-8") as f:
        content = f.read()

    restore_rules = [
        (r'(Zis\s*=\s*)"' + _LOCAL_PROXY_PATTERN + r'"', f'\\1"{OFFICIAL_ENDPOINTS["Zis"]}"'),
        (r'(Dis\s*=\s*)"' + _LOCAL_PROXY_PATTERN + r'"', f'\\1"{OFFICIAL_ENDPOINTS["Dis"]}"'),
        (r'(Uis\s*=\s*)"' + _LOCAL_PROXY_PATTERN + r'"', f'\\1"{OFFICIAL_ENDPOINTS["Uis"]}"'),
        (r'(Gis\s*=\s*)"' + _LOCAL_PROXY_PATTERN + r'"', f'\\1"{OFFICIAL_ENDPOINTS["Gis"]}"'),
    ]

    modified_content = content
    total_replaced = 0

    for pattern, repl in restore_rules:
        modified_content, count = re.subn(pattern, repl, modified_content)
        total_replaced += count

    if total_replaced == 0:
        print("[-] 未发现需要还原的本地代理端点。")
        return

    with open(main_js_path, "w", encoding="utf-8") as f:
        f.write(modified_content)

    print(f"[√] 已恢复官方端点，共回滚 {total_replaced} 处设置。")
    kill_language_server()


def main():
    print("=" * 60)
    print("      Antigravity IDE 端点修补/还原工具")
    print("=" * 60)

    raw_dir = input("请输入 Antigravity IDE 安装目录 (直接粘贴路径后回车): ").strip()
    if not raw_dir:
        print("[-] 输入路径为空，程序退出。")
        sys.exit(1)

    main_js_path = locate_main_js(raw_dir)
    if not main_js_path:
        print(f"[-] 错误: 无法在给定的目录下找到 main.js")
        print(f"    查找路径预期为: <安装目录>\\resources\\app\\out\\main.js")
        sys.exit(1)

    print(f"[+] 成功定位目标文件: {main_js_path}")
    print(f"[+] 当前反代地址: {DEFAULT_PROXY_URL} (来自 .env 的 HOST/PORT)")
    print("\n请选择操作:")
    print(f"  [1] 切换为本地反代 ({HOST}:{PORT})")
    print("  [2] 恢复为 Google 官方端点")
    print("  [3] 仅清理后台常驻 Language Server 守护进程")

    choice = input("请输入选项 (1/2/3) [默认 1]: ").strip()
    if choice == "" or choice == "1":
        patch_file(main_js_path, DEFAULT_PROXY_URL)
    elif choice == "2":
        restore_file(main_js_path)
    elif choice == "3":
        kill_language_server()
    else:
        print("[-] 无效选择，已取消操作。")


if __name__ == "__main__":
    main()