#!/usr/bin/env python3
"""把 Bettbox (Flutter) 源码适配为可编译的 iOS 工程。

用法: python3 patch_ios.py <源码根目录> <appicons.zip 路径>
在 macOS runner 上、flutter create --platforms=ios 之后、flutter build ios 之前运行。
"""
import os
import re
import sys
import json
import plistlib
import zipfile

DISABLE_RUST = os.environ.get("DISABLE_RUST") == "1"

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
ICONS_ZIP = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else ""
CORE_OK = os.path.exists(os.path.join(ROOT, "ios", "Runner", "libmeta.a"))
print("[patch] ROOT =", ROOT)
print("[patch] Go 静态内核 libmeta.a :", "已编译，将链接进 Runner" if CORE_OK else "缺失，仅构建 Flutter 壳")


def read(p):
    with open(os.path.join(ROOT, p), encoding="utf-8") as f:
        return f.read()


def write(p, s):
    with open(os.path.join(ROOT, p), "w", encoding="utf-8") as f:
        f.write(s)


def ensure_io_import(s):
    if "import 'dart:io';" not in s and 'import "dart:io";' not in s:
        s = "import 'dart:io';\n" + s
    return s


def sub(p, old, new, tag):
    s = read(p)
    if old not in s:
        print("[patch] !! 未匹配:", tag)
        return False
    write(p, s.replace(old, new, 1))
    print("[patch] ok:", tag)
    return True


# ---------------------------------------------------------------- Dart: FFI 内核
p = "lib/clash/lib.dart"
s = ensure_io_import(read(p))
# iOS 上 Go 内核以静态库 (-force_load) 链接进主可执行文件，符号从主进程解析
s = s.replace(
    "lib = DynamicLibrary.open('libmeta.so');",
    "lib = Platform.isIOS\n        ? DynamicLibrary.process()\n        : DynamicLibrary.open('libmeta.so');",
)
# 内核符号缺失时不让构造函数抛异常导致启动崩溃
s = s.replace(
    "    clashFFI.initNativeApiBridge(NativeApi.initializeApiDLData);",
    "    try {\n"
    "      clashFFI.initNativeApiBridge(NativeApi.initializeApiDLData);\n"
    "    } catch (e) {\n"
    "      print('[ios] initNativeApiBridge unavailable: $e');\n"
    "    }",
)
# 让 clashLib 在 iOS 主 isolate 也可用
s = s.replace(
    "ClashLib? get clashLib =>\n    system.isAndroid && !globalState.isService ? ClashLib() : null;",
    "ClashLib? get clashLib =>\n    (system.isAndroid || Platform.isIOS) && !globalState.isService\n        ? ClashLib()\n        : null;",
)
write(p, s)
print("[patch] ok: lib.dart FFI / clashLib getter")

# iOS 上用本地 ReceivePort 闭环替换 Android Service isolate 的 IPC
IOS_SERVICE_CODE = """
  void _initIosService() {
    try {
      _registerMainPort(receiverPort.sendPort);
      if (!_isListening) {
        _isListening = true;
        receiverPort.listen((message) {
          if (message is SendPort) {
            sendPort = message;
            if (!_canSendCompleter.isCompleted) {
              _canSendCompleter.complete(true);
            }
          } else if (message is String) {
            handleResultMessage(message);
          }
        });
      }
      final handler = ClashLibHandler();
      final servicePort = ReceivePort();
      servicePort.listen((message) async {
        try {
          final res = await handler.invokeAction(message as String);
          receiverPort.sendPort.send(res);
        } catch (e) {
          commonPrint.warn('[ios] invokeAction failed: $e');
          try {
            receiverPort.sendPort.send('');
          } catch (_) {}
        }
      });
      sendPort = servicePort.sendPort;
      final messagePort = ReceivePort();
      try {
        handler.attachMessagePort(messagePort.sendPort.nativePort);
        messagePort.listen((message) {
          try {
            receiverPort.sendPort.send(message);
          } catch (_) {}
        });
      } catch (e) {
        commonPrint.warn('[ios] attachMessagePort failed: $e');
      }
      if (!_canSendCompleter.isCompleted) {
        _canSendCompleter.complete(true);
      }
    } catch (e) {
      commonPrint.warn('[ios] ios service init failed: $e');
      if (!_canSendCompleter.isCompleted) {
        _canSendCompleter.complete(false);
      }
    }
  }

"""
if "void _initIosService()" not in s:
    s = read(p)
    anchor = "  Future<void> _initService() async {"
    if anchor in s:
        s = s.replace(anchor, IOS_SERVICE_CODE + anchor, 1)
        s = s.replace(
            anchor,
            anchor
            + "\n    if (Platform.isIOS) {\n      _initIosService();\n      return;\n    }",
            1,
        )
        write(p, s)
        print("[patch] ok: lib.dart iOS 本地 IPC 闭环")
    else:
        print("[patch] !! 未匹配: _initService 锚点")

# ---------------------------------------------------------------- Dart: 内核选择
sub(
    "lib/clash/core.dart",
    "    if (system.isAndroid) {\n      clashInterface = clashLib!;",
    "    if (system.isAndroid || Platform.isIOS) {\n      clashInterface = clashLib!;",
    "core.dart iOS 走 FFI 内核",
)
s = read("lib/clash/core.dart")
write("lib/clash/core.dart", ensure_io_import(s))

# ---------------------------------------------------------------- Dart: 启动流程容错
p = "lib/main.dart"
s = ensure_io_import(read(p))
old_wait = """  final version = await system.version;
  await Future.wait([
    globalState.initApp(version),
    clashCore.preload(),
    loadBundledRouteIcons(),
  ]);"""
new_wait = """  final version = await system.version;
  await globalState.initApp(version);
  await loadBundledRouteIcons();
  try {
    await clashCore.preload().timeout(const Duration(seconds: 15));
  } catch (e) {
    print('[ios] core preload skipped: $e');
  }"""
if old_wait in s:
    s = s.replace(old_wait, new_wait, 1)
    print("[patch] ok: main.dart preload 超时保护")
else:
    print("[patch] !! 未匹配: main.dart Future.wait")
s = s.replace(
    "  await ensureRustLib();",
    "  try {\n"
    "    await ensureRustLib().timeout(const Duration(seconds: 20));\n"
    "  } catch (e) {\n"
    "    print('[ios] rust lib skipped: $e');\n"
    "  }",
    1,
)
write(p, s)

# ---------------------------------------------------------------- Dart: 跳过 Android 专属 UI 层
p = "lib/application.dart"
s = ensure_io_import(read(p))
s = s.replace(
    "    return AndroidManager(",
    "    if (Platform.isIOS) {\n      return child;\n    }\n    return AndroidManager(",
    1,
)
write(p, s)
print("[patch] ok: application.dart 跳过 AndroidManager")

# ---------------------------------------------------------------- 可选: 禁用 code_forge 的 Rust 编译
if DISABLE_RUST:
    p = "plugins/code_forge/ios/code_forge.podspec"
    s = read(p)
    s = re.sub(r"\n\s*s\.script_phase = \{.*?\n\s*\}\n", "\n", s, flags=re.S)
    s = re.sub(r"\n\s*'OTHER_LDFLAGS' =>[^\n]*\n", "\n", s)
    write(p, s)
    print("[patch] ok: 禁用 code_forge Rust build phase（脚本引擎功能降级，保证可出包）")

# ---------------------------------------------------------------- iOS 工程配置
pbx = "ios/Runner.xcodeproj/project.pbxproj"
s = read(pbx)
s = re.sub(r"PRODUCT_BUNDLE_IDENTIFIER = [^;]+;", "PRODUCT_BUNDLE_IDENTIFIER = com.appshubcc.Bettbox;", s)
s = re.sub(r"IPHONEOS_DEPLOYMENT_TARGET = [^;]+;", "IPHONEOS_DEPLOYMENT_TARGET = 15.0;", s)
# 移除模板里可能已有的 OTHER_LDFLAGS 定义，避免覆盖我们注入的链接参数
s = re.sub(r"\n\s*OTHER_LDFLAGS\s*=\s*[^;]+;", "", s)
if CORE_OK and "-force_load" not in s:
    ld = (
        '\t\t\t\tOTHER_LDFLAGS = (\n'
        '\t\t\t\t\t"$(inherited)",\n'
        '\t\t\t\t\t"-force_load",\n'
        '\t\t\t\t\t"$(SRCROOT)/Runner/libmeta.a",\n'
        '\t\t\t\t\t"-lc++",\n'
        '\t\t\t\t\t"-lresolv",\n'
        '\t\t\t\t\t"-framework",\n'
        '\t\t\t\t\t"Security",\n'
        '\t\t\t\t\t"-framework",\n'
        '\t\t\t\t\t"CoreFoundation",\n'
        '\t\t\t\t);\n'
    )
    # 给每个 buildSettings 块注入（pbxproj 层级的 $(inherited) 会保留 Pods 的链接参数）
    s, n = re.subn(r"(buildSettings = \{\n)", lambda m: m.group(1) + ld, s)
    print("[patch] ok: pbxproj 注入 OTHER_LDFLAGS, 块数 =", n)
write(pbx, s)
print("[patch] ok: pbxproj bundle id / deployment target")

# Podfile 平台版本
pod = "ios/Podfile"
if os.path.exists(os.path.join(ROOT, pod)):
    s = read(pod)
    s = re.sub(r"^#\s*platform :ios,.*$", "platform :ios, '15.0'", s, flags=re.M)
    if "platform :ios, '15.0'" not in s:
        s = "platform :ios, '15.0'\n" + s
    write(pod, s)
    print("[patch] ok: Podfile iOS 15.0")

# Info.plist
pl = "ios/Runner/Info.plist"
with open(os.path.join(ROOT, pl), "rb") as f:
    info = plistlib.load(f)
info["CFBundleDisplayName"] = "Bettbox"
info["CFBundleName"] = "Bettbox"
info["ITSAppUsesNonExemptEncryption"] = False
info["NSCameraUsageDescription"] = "用于扫描二维码导入订阅配置"
info["NSPhotoLibraryUsageDescription"] = "用于选择图片导入配置或备份"
info["NSAppTransportSecurity"] = {"NSAllowsArbitraryLoads": True}
info["UIRequiredDeviceCapabilities"] = ["arm64"]
with open(os.path.join(ROOT, pl), "wb") as f:
    plistlib.dump(info, f)
print("[patch] ok: Info.plist (名称 / 权限 / 加密声明)")

# AppIcon
if ICONS_ZIP and os.path.exists(ICONS_ZIP):
    dst = os.path.join(ROOT, "ios", "Runner", "Assets.xcassets", "AppIcon.appiconset")
    os.makedirs(dst, exist_ok=True)
    with zipfile.ZipFile(ICONS_ZIP) as z:
        z.extractall(dst)
    print("[patch] ok: AppIcon 已替换为 Bettbox 不透明图标")
else:
    print("[patch] !! 缺少 appicons.zip，使用 Flutter 默认图标")

print("[patch] DONE")
