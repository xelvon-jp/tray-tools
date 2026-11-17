"""おどうぐばこ (odougubako) のコマンドライン入口。

odougubako_core の機能を、ホスト (PHAL など) や人から
呼べる形にしたもの。AI との連携は持たない (窓口は PHAL)。

使い方:
  odougubako.py register <登録ファイル> [--by 名前]
  odougubako.py run <道具名> [引数...]
  odougubako.py list [--json]
  odougubako.py describe <道具名>
  odougubako.py eval <道具名> [--usability N] [--findability N]
                 [--expectation N] [--memo 文] [--command-id ID]
  odougubako.py rollback <道具名>
  odougubako.py remove <道具名>
  odougubako.py gui
"""

import argparse
import json
import sys
from pathlib import Path

import odougubako_core as core

EVAL_LABELS = {"usability": "使いやすさ", "findability": "選びやすさ",
               "expectation": "期待どおりか"}


def _print_json(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=1))


def cmd_register(args) -> int:
    path = Path(args.file)
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ファイルが読めません: {exc}")
        return 1
    result = core.register_source(source, created_from=path.name,
                                  created_by=args.by or "")
    if args.json:
        _print_json(result)
    elif result["ok"]:
        tool = result["tool"]
        print(f"登録しました: {tool['name']} "
              f"(分類: {tool['category']}, 版: {tool['version']})")
        print(f"説明: {tool['description']}")
    else:
        print(f"登録できませんでした: {result['reason']}")
    return 0 if result["ok"] else 1


def cmd_run(args) -> int:
    result = core.run_tool(args.name, args.tool_args)
    if args.json:
        _print_json(result)
        return 0 if result["ok"] else 1
    if result["stdout"]:
        print(result["stdout"], end="")
    if result["stderr"]:
        print(result["stderr"], end="", file=sys.stderr)
    if result["reason"]:
        print(result["reason"])
    elif not result["ok"]:
        print(f"終了コード {result['exit_code']} ({result['seconds']}秒)")
    return 0 if result["ok"] else 1


def _format_tool_line(tool) -> str:
    rate = tool["success_rate"]
    rate_text = f"{rate:.0%}" if rate is not None else "-"
    averages = tool["eval"]["averages"]
    eval_text = ""
    if averages:
        parts = [f"{EVAL_LABELS.get(k, k)}{v:g}"
                 for k, v in averages.items()]
        eval_text = " 評価 " + " ".join(parts)
    dormant = " 休眠" if tool["dormant"] else ""
    return (f"{tool['name']} [{tool['category']}] v{tool['version']} "
            f"使用{tool['count']} 成功率{rate_text}{eval_text}{dormant}\n"
            f"  {tool['description']}")


def cmd_list(args) -> int:
    tools = core.list_tools()
    if args.json:
        _print_json({"tools": tools})
        return 0
    if not tools:
        print("おどうぐばこは空です")
        return 0
    for tool in tools:
        print(_format_tool_line(tool))
    return 0


def cmd_describe(args) -> int:
    text = core.describe(args.name)
    if not text:
        print(f"道具 '{args.name}' がありません")
        return 1
    print(text, end="")
    return 0


def cmd_eval(args) -> int:
    scores = {}
    for key in core.EVAL_KEYS:
        value = getattr(args, key)
        if value is not None:
            scores[key] = value
    result = core.record_eval(args.name, scores, memo=args.memo or "",
                              command_id=args.command_id or "")
    if args.json:
        _print_json(result)
    elif result["ok"]:
        print(f"評価を記録しました: {args.name}")
    else:
        print(f"評価を記録できませんでした: {result['reason']}")
    return 0 if result["ok"] else 1


def cmd_rollback(args) -> int:
    result = core.rollback(args.name)
    print("前版へ戻しました" if result["ok"]
          else f"戻せませんでした: {result['reason']}")
    return 0 if result["ok"] else 1


def cmd_remove(args) -> int:
    result = core.remove_tool(args.name)
    print("削除しました" if result["ok"]
          else f"削除できませんでした: {result['reason']}")
    return 0 if result["ok"] else 1


def cmd_gui(args) -> int:
    import odougubako_gui

    return odougubako_gui.main()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="odougubako")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("register")
    p.add_argument("file")
    p.add_argument("--by", default="")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_register)

    p = sub.add_parser("run")
    p.add_argument("name")
    p.add_argument("tool_args", nargs="*")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("list")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("describe")
    p.add_argument("name")
    p.set_defaults(func=cmd_describe)

    p = sub.add_parser("eval")
    p.add_argument("name")
    p.add_argument("--usability", type=int)
    p.add_argument("--findability", type=int)
    p.add_argument("--expectation", type=int)
    p.add_argument("--memo", default="")
    p.add_argument("--command-id", default="")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("rollback")
    p.add_argument("name")
    p.set_defaults(func=cmd_rollback)

    p = sub.add_parser("remove")
    p.add_argument("name")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("gui")
    p.set_defaults(func=cmd_gui)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
