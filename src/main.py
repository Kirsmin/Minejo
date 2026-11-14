# Minejo!
import sys, argparse
from pathlib import Path

import log
from mojang import EXTRACTORS, Mojang
from net import RetryExhausted
from resource import Resource
from store import AlreadyRunning, run_lock

if __name__ != '__main__': sys.exit(1)

API_PER_MINUTE = 10

LABELS = {
    "missing": "缺失",
    "corrupt": "损坏",
    "orphan": "多余",
    "unreferenced": "未被引用",
    "stale_temp": "临时残留",
    "manifest_drift": "清单不一致",
}


def show(report):
    for key, items in vars(report).items():
        if items:
            log.warn(f"{LABELS[key]} {len(items)} 项，例如: {', '.join(items[:3])}")
    if report.ok:
        log.info("检查通过")


def mojang(resource, args):
    return Mojang(resource, max_workers=args.workers, api_per_minute=args.api_per_minute)


def sync(args):
    log.info("开始同步 Mojang 版本")
    with run_lock(args.root), Resource(args.root, EXTRACTORS) as resource, \
            mojang(resource, args) as client:
        updated = client.sync(args.versions or None)
    log.info(f"同步完成，本次更新 {len(updated)} 个版本")
    return 0


def check(args):
    with Resource(args.root, EXTRACTORS) as resource:
        report = resource.Check(args.deep, args.limit)
    show(report)
    return 0 if report.ok else 1


def repair(args):
    with run_lock(args.root), Resource(args.root, EXTRACTORS) as resource, \
            mojang(resource, args) as client:
        report, count = client.repair(args.deep, args.limit)
    show(report)
    log.info(f"修复完成，重新下载 {count} 个资源")
    return 0


def gc(args):
    with run_lock(args.root), Resource(args.root, EXTRACTORS) as resource:
        hashes = resource.GC(args.days, dry_run=not args.apply)
    log.info(f"{'已回收' if args.apply else '可回收（加 --apply 执行）'} {len(hashes)} 个资源")
    return 0


def rebuild(args):
    with run_lock(args.root), Resource(args.root, EXTRACTORS) as resource:
        report = resource.RebuildIndex(args.deep)
    show(report)
    log.info("索引重建完成")
    return 0


COMMANDS = {"sync": sync, "check": check, "repair": repair, "gc": gc, "rebuild": rebuild}


def build_parser():
    parser = argparse.ArgumentParser(description="Minejo! Minecraft 游戏资源管理器")
    parser.add_argument("--root", default="Resource", help="资源目录")
    parser.add_argument("--quiet", action="store_true", help="日志简略输出")
    parser.add_argument("--workers", type=int, default=4, help="并发下载数（1-8）")
    parser.add_argument("--api-per-minute", type=int, default=API_PER_MINUTE)
    parser.set_defaults(command="sync", versions=[])
    commands = parser.add_subparsers(dest="command")

    commands.add_parser("sync", help="同步版本（默认命令）").add_argument(
        "versions", nargs="*", help="版本 id，省略则全量"
    )
    for name, help in (("check", "检查完整性"), ("repair", "修复缺失与损坏的资源")):
        sub = commands.add_parser(name, help=help)
        sub.add_argument("--deep", action="store_true", help="重算 SHA-1")
        sub.add_argument("--limit", type=int, help="深度校验的最大文件数")
    sub = commands.add_parser("gc", help="回收未被引用的资源")
    sub.add_argument("--days", type=int, default=7, help="宽限天数")
    sub.add_argument("--apply", action="store_true", help="实际删除，默认只列出")
    commands.add_parser("rebuild", help="重建 Index.db").add_argument(
        "--deep", action="store_true", help="重算 SHA-1"
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    log.setup(Path(args.root) / "Log.txt", args.quiet)
    try:
        return COMMANDS[args.command](args)
    except AlreadyRunning:
        log.warn("已有任务正在运行，本次退出")
        return 0
    except RetryExhausted as error:
        log.error(f"网络重试耗尽，退出: {error}")
        return 1
    except KeyboardInterrupt:
        log.warn("已中断")
        return 130
    except Exception as error:
        log.error(f"{type(error).__name__}: {error}", exc=True)
        return 1


sys.exit(main())
