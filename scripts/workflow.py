#!/usr/bin/env python3
"""Offline, evidence-based delivery checklist. No device or network operations."""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from datetime import date, datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = 1
CHOICES = {
    "computer": ("mac", "windows", "none"),
    "phone": ("android", "iphone", "none"),
    "goal": ("free", "subscription"),
    "network": ("self-hosted", "existing"),
}
RESULTS = ("passed", "failed", "pending", "accepted")
LIMITS = {"computer.disconnect_limit", "phone.disconnect_limit"}

# These statements describe required observations, not commands or assumed facts.
STAGES = [
    ("intake", "接单条件", "SKILL.md", [
        ("intake.eligibility", "本人实际所在地符合服务支持范围；出口 IP 不能代替资格"),
        ("intake.tool_access", "已确认可用 AI 工具、设备访问权限和本人配合方式"),
        ("intake.account_plan", "已有账号或本人准备账号的路径明确，不记录登录资料"),
        ("intake.existing_setup", "已了解旧网络、已有配置和需保留的数据；不盲目清理"),
        ("intake.existing_network", "已有网络/节点在当前设备可用，管理或售后路径明确；不另行部署服务器"),
        ("intake.scope", "设备、交付终点、第三方费用及服务范围已向客户讲清"),
    ]),
    ("payment-preflight", "费用发生前核对付款条件", "references/accounts-payment.md", [
        ("payment.method", "本人已有该渠道支持、所需资料齐全的付款工具；只有账号或待补资料不算完成"),
        ("payment.channel", "已区分官网、Google Pay、Google Play 或 Apple 内购路径"),
        ("payment.costs", "已说明订阅与第三方费用、可能验证及自动续费；客户本人付款"),
    ]),
    ("server", "服务器与节点", "references/network.md", [
        ("server.access", "本人服务器或已有节点可管理；涉及重装时先备份并获明确授权"),
        ("server.runtime", "服务端进程、监听和必要网络放行已实际读回核对"),
        ("server.node", "已导出并核对本客户节点参数，凭据只保存在客户本机"),
    ]),
    ("computer", "电脑网络", "references/platforms.md", [
        ("computer.settings", "当前客户端、TUN、路由模式和系统代理状态逐项观察"),
        ("computer.local_proxy", "实际 HTTP 入口已核对，不照抄其他客户端口"),
        ("computer.exit", "系统路径及指定代理抽样出口已比对客户自己的基线"),
        ("computer.handoff", "网络切换后普通网页与 AI 新回复可用，原配置可恢复"),
        ("computer.disconnect_limit", "断线行为已验证，或客户明确接受记录中的未验证/直连限制"),
    ]),
    ("browser", "专用浏览器", "references/platforms.md", [
        ("browser.profile", "已建立并打开本客户专用环境，未覆盖私人浏览器"),
        ("browser.proxy", "该浏览器实际 HTTP 代理入口与出口已核对"),
        ("browser.settings", "浏览器时区、语言、位置权限按客户选择核对"),
        ("browser.network", "DNS、IPv6、WebRTC 已按适用范围检查，结果及局限讲清"),
    ]),
    ("phone", "手机网络", "references/platforms.md", [
        ("phone.client", "官方客户端来源、正确节点导入和系统 VPN 状态已核对"),
        ("phone.routing", "全局路由与排除项已观察，未把 VPN 图标等同所有流量验证"),
        ("phone.exit", "在手机当前 Wi-Fi 或蜂窝网络独立检查出口与 HTTPS"),
        ("phone.background", "后台/省电限制与重连方法按实际手机设置核对"),
        ("phone.disconnect_limit", "手机断线行为已验证，或客户明确接受具体限制"),
    ]),
    ("prelogin", "登录前复查", "references/accounts-payment.md", [
        ("prelogin.network", "使用当前网络重新核对出口和适用检查结果，无未处理连接故障"),
        ("prelogin.clock", "实际系统时间正确，电脑/手机/浏览器时区按已选方案核对"),
        ("prelogin.account", "本人可以使用自己的邮箱/账号及所需验证方式，未交出凭据"),
    ]),
    ("login", "登录", "references/accounts-payment.md", [
        ("login.session", "本人完成验证后，Claude 进入已登录会话并获得一次新回复；不是仅见登录或授权页"),
        ("login.same_account", "所需设备使用同一目标账号；无需把实际邮箱记入日志"),
    ]),
    ("subscription", "本人订阅与权益", "references/accounts-payment.md", [
        ("subscription.payment", "客户本人确认并完成真实订阅付款，绑卡/预授权不算完成"),
        ("subscription.receipt", "付款回执和订单状态已由本人核对，不保存卡号和订单隐私"),
        ("subscription.entitlement", "目标账号实际显示正确付费权益，所需设备已同步核对"),
        ("subscription.renewal", "本人已了解账期、续费金额及取消入口；未发生续费不称已验证"),
    ]),
    ("handover", "交付", "SKILL.md", [
        ("handover.startup", "客户知道正确启动入口，重新打开应用后已检查使用状态"),
        ("handover.recovery", "恢复原网络和常见故障的步骤已交给客户"),
        ("handover.panel_choice", "已询问是否需要自检面板；选择不安装也可完成本项"),
        ("handover.support", "已告知交付后 14 天答疑与最多 2 次预约远程的约定范围"),
        ("handover.privacy", "临时敏感材料已由客户妥善保管/清理，公开文件不含个人信息"),
    ]),
]
CHECKS = {cid: (sid, text) for sid, _, _, rows in STAGES for cid, text in rows}


class WorkflowError(Exception):
    pass


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_note(value):
    """Reject common secrets before persistence; do not reproduce offending input."""
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise WorkflowError("观察说明须为 1–500 字的非空脱敏文字。")
    if any(ord(c) < 32 for c in value):
        raise WorkflowError("观察说明不能含控制字符或多行内容。")
    patterns = [
        r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
        r"(?:https?|vless|vmess|trojan|ss|otpauth)://",
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}\b",
        r"\b[A-Za-z0-9_+/=-]{24,}\b",
        r"(?:/Users/|/home/|[A-Za-z]:\\Users\\)",
    ]
    if any(re.search(pattern, value) for pattern in patterns):
        raise WorkflowError("观察说明含疑似个人信息、凭据或链接，请仅记录脱敏结论。")
    # A valid calendar date is ordinary evidence, not a card number or OTP.
    # Mask only dates for the numeric check; credential values below stay intact.
    def mask_date(match):
        try:
            date.fromisoformat(match.group())
            return "[日期]"
        except ValueError:
            return match.group()
    numeric_text = re.sub(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)", mask_date, value)
    if re.search(r"(?:\d[ -]?){6,}", numeric_text):
        raise WorkflowError("观察说明含疑似个人信息、凭据或链接，请仅记录脱敏结论。")
    credential = r"(?i)(?:password|passwd|token|secret|private[_ -]?key|cvv|cvc|otp|验证码|密码|密钥|安全码|卡号)\s*[:：=]\s*(.+)"
    redacted_status = (
        r"(?:(?:已)?(?:由)?(?:客户|用户)?本人(?:输入|操作|保存|保管|处理)"
        r"(?:[，,；;](?:不|未)(?:保存|记录|收集))?|"
        r"(?:不|未)(?:保存|记录|收集)|待本人补齐|尚未提供|缺失)[。.]?"
    )
    for match in re.finditer(credential, value):
        if not re.fullmatch(redacted_status, match.group(1).strip()):
            raise WorkflowError("观察说明含疑似个人信息、凭据或链接，请仅记录脱敏结论。")
    for token in re.findall(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])|[0-9A-Fa-f:]*:[0-9A-Fa-f:]+", value):
        try:
            ipaddress.ip_address(token)
        except ValueError:
            continue
        raise WorkflowError("观察说明不能保存实际 IP 地址，请写‘与本机基线一致’等结论。")
    return value.strip()


def stages_for(profile):
    stages = []
    for sid, title, document, rows in STAGES:
        if (
            (sid in ("payment-preflight", "subscription") and profile["goal"] == "free")
            or (sid == "phone" and profile["phone"] == "none")
            or (sid in ("computer", "browser") and profile["computer"] == "none")
            or (sid == "server" and profile.get("network", "self-hosted") == "existing")
        ):
            continue
        applicable = [(cid, text) for cid, text in rows if
                      cid != "intake.existing_network" or profile.get("network", "self-hosted") == "existing"]
        stages.append((sid, title, document, applicable))
    return stages


def success(cid, observation):
    return observation and (observation["result"] == "passed" or (
        cid in LIMITS and observation["result"] == "accepted" and observation["customer_accepted"]
    ))


def validate(state):
    if not isinstance(state, dict) or set(state) != {"schema", "profile", "observations", "updated_at"}:
        raise WorkflowError("进度文件格式不受支持，请保留原文件后检查，未覆盖数据。")
    if type(state["schema"]) is not int or state["schema"] != SCHEMA:
        raise WorkflowError("进度文件版本不受支持，未覆盖数据。")
    profile = state["profile"]
    # Schema 1 originally had no network field and always required a server.
    # Interpret that route as self-hosted without rewriting a file on read.
    if isinstance(profile, dict) and set(profile) == {"computer", "phone", "goal"}:
        state = dict(state)
        profile = state["profile"] = dict(profile, network="self-hosted")
    if not isinstance(profile, dict) or set(profile) != set(CHOICES):
        raise WorkflowError("进度中的设备资料无效。")
    if any(profile[key] not in options for key, options in CHOICES.items()):
        raise WorkflowError("进度中的设备或交付目标无效。")
    if profile["computer"] == "none" and profile["phone"] == "none":
        raise WorkflowError("至少选择一台电脑或手机，不能同时为 none。")
    records = state["observations"]
    if not isinstance(records, dict):
        raise WorkflowError("进度中的观察记录无效。")
    allowed = {cid for stage in stages_for(profile) for cid, _ in stage[3]}
    for cid, obs in records.items():
        if cid not in allowed or not isinstance(obs, dict) or set(obs) != {"result", "note", "observed_at", "customer_accepted"}:
            raise WorkflowError("进度包含未知或不适用的检查项。")
        if obs["result"] not in RESULTS or type(obs["customer_accepted"]) is not bool:
            raise WorkflowError("进度中的检查结果无效。")
        if obs["result"] == "accepted" and (cid not in LIMITS or not obs["customer_accepted"]):
            raise WorkflowError("此检查项不允许以接受限制代替通过。")
        if obs["result"] != "accepted" and obs["customer_accepted"]:
            raise WorkflowError("进度中的接受限制标记不一致。")
        safe_note(obs["note"])
        check_time(obs["observed_at"])
    check_time(state["updated_at"])
    # A future stage cannot contain observations while a previous stage is incomplete.
    incomplete = False
    for stage in stages_for(profile):
        if incomplete and any(cid in records for cid, _ in stage[3]):
            raise WorkflowError("进度存在跳过前置阶段的记录，请先核对原文件。")
        if not all(success(cid, records.get(cid)) for cid, _ in stage[3]):
            incomplete = True
    return state


def check_time(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError()
    except (ValueError, TypeError):
        raise WorkflowError("进度中的时间格式无效。")


class Workflow:
    def __init__(self, state_dir):
        requested = Path(state_dir).expanduser().absolute()
        # Do not follow a link into another client's state or a protected file.
        # Parent system aliases such as macOS /var and /tmp are resolved normally.
        if requested.is_symlink():
            raise WorkflowError("进度目录及文件不能是符号链接。")
        self.directory = requested.resolve()
        self.path = self.directory / "progress.json"
        self.check_paths()

    def check_paths(self):
        if self.directory.is_symlink() or self.path.is_symlink():
            raise WorkflowError("进度目录及文件不能是符号链接。")
        if self.directory.exists() and not self.directory.is_dir():
            raise WorkflowError("进度目录不是文件夹。")
        if self.path.exists() and not self.path.is_file():
            raise WorkflowError("进度文件位置已被其他类型的文件占用。")

    def load(self):
        self.check_paths()
        if not self.path.exists():
            raise WorkflowError("尚未初始化；请先运行 init 并指定设备与目标。")
        try:
            if self.path.stat().st_size > 100000:
                raise WorkflowError("进度文件过大，未读取或覆盖。")
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise WorkflowError("无法读取进度文件；请保留原文件排查，未重建或覆盖。")
        return validate(state)

    def save(self, state):
        self.check_paths()
        state["updated_at"] = utcnow()
        validate(state)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=".progress-", suffix=".tmp", dir=str(self.directory))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def init(self, profile):
        profile = dict(profile)
        profile.setdefault("network", "self-hosted")
        self.check_paths()
        if self.path.exists():
            current = self.load()
            if current["profile"] != profile:
                raise WorkflowError("已有不同设备/目标的进度。请用 configure 明确变更，或换一个 --state-dir。")
            return self.view(current)
        state = {"schema": SCHEMA, "profile": profile, "observations": {}, "updated_at": utcnow()}
        self.save(state)
        return self.view(state)

    def view(self, state=None):
        state = self.load() if state is None else validate(state)
        records = state["observations"]
        stages = stages_for(state["profile"])
        current = next((s for s in stages if not all(success(cid, records.get(cid)) for cid, _ in s[3])), None)
        accepted = [{"id": cid, "reason": obs["note"]} for cid, obs in records.items() if obs["result"] == "accepted"]
        skipped = [sid for sid, _, _, _ in STAGES if sid not in {s[0] for s in stages}]
        base = {
            "profile": state["profile"], "stage": current[0] if current else "complete",
            "stage_title": current[1] if current else "记录中的约定交付已完成",
            "document": current[2] if current else "SKILL.md",
            "status": "needs_observation" if current else "complete",
            "accepted_limits": accepted, "skipped_stages": skipped,
            "evidence_boundary": "本程序只核对已记录观察及前后关系，不自行验证设备、付款、服务资格或账号安全。",
            "updated_at": state["updated_at"],
        }
        checks = []
        if current:
            for cid, text in current[3]:
                obs = records.get(cid)
                checks.append({"id": cid, "description": text, "result": obs["result"] if obs else "pending", "satisfied": bool(success(cid, obs)), "acceptance_allowed": cid in LIMITS, "note": obs["note"] if obs else None})
            if any(c["result"] == "failed" for c in checks):
                base["status"] = "blocked"
        base["checks"] = checks
        base["missing"] = [c["id"] for c in checks if not c["satisfied"]]
        base["completed_stages"] = [s[0] for s in stages if all(success(cid, records.get(cid)) for cid, _ in s[3])]
        return base

    def record(self, cid, result, note, accepted=False):
        state = self.load()
        view = self.view(state)
        if result not in RESULTS or cid not in CHECKS:
            raise WorkflowError("未知检查项或结果；请运行 next 查看当前阶段。")
        note = safe_note(note)
        if result == "accepted" and (cid not in LIMITS or not accepted):
            raise WorkflowError("仅断线限制项可在客户明确接受后使用 accepted，并附 --accept-limit 与原因。")
        if result != "accepted" and accepted:
            raise WorkflowError("--accept-limit 只能与 accepted 一起使用。")
        current_ids = {c["id"] for c in view["checks"]}
        if cid not in current_ids and cid not in state["observations"]:
            raise WorkflowError("只能记录当前阶段，或更正已有记录；不能跳过前置阶段。")
        existing = state["observations"].get(cid)
        if existing and (existing["result"], existing["note"], existing["customer_accepted"]) == (result, note, accepted):
            return self.view(state)
        # Re-observing or correcting a past stage invalidates every downstream result.
        target_index = next(i for i, s in enumerate(STAGES) if s[0] == CHECKS[cid][0])
        downstream = {s[0] for s in STAGES[target_index + 1:]}
        state["observations"] = {key: val for key, val in state["observations"].items() if CHECKS[key][0] not in downstream}
        state["observations"][cid] = {"result": result, "note": note, "observed_at": utcnow(), "customer_accepted": accepted}
        self.save(state)
        return self.view(state)

    def configure(self, changes):
        state = self.load()
        profile = dict(state["profile"])
        changed = []
        for key, value in changes.items():
            if key not in CHOICES or value not in CHOICES[key]:
                raise WorkflowError("设备或目标参数无效。")
            if profile[key] != value:
                changed.append(key)
                profile[key] = value
        if not changed:
            return self.view(state)
        # Scope and tool readiness change with any new device/goal; repeat intake.
        # Keep the new profile but remove all observations so no prior facts bypass it.
        removed = len(state["observations"])
        state["profile"] = profile
        state["observations"] = {}
        self.save(state)
        result = self.view(state)
        result["invalidated_count"] = removed
        result["configuration_notice"] = "设备、网络或目标变化后须重新确认账本观察；本程序不会修改已有配置、登录状态或免费权益，也不要求重装。"
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="客户交付的离线观察记录；不会连接网络、配置设备或执行付款。")
    parser.add_argument("--state-dir", default=str(ROOT / ".local"), help="每位客户使用独立的本地进度目录")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("init", help="初始化；不会覆盖现有不同配置")
    config = sub.add_parser("configure", help="变更设备/网络/目标；只撤销账本观察，不修改设备")
    for key, values in CHOICES.items():
        options = {"default": "self-hosted"} if key == "network" else {"required": True}
        start.add_argument("--" + key, choices=values, **options)
        config.add_argument("--" + key, choices=values)
    sub.add_parser("next", help="显示下一阶段及需要的观察")
    sub.add_parser("status", help="显示进度与已接受的具体限制")
    record = sub.add_parser("record", help="保存脱敏观察；更正前面阶段会使后续结果失效")
    record.add_argument("check_id")
    record.add_argument("--result", choices=RESULTS, required=True)
    record.add_argument("--note", required=True)
    record.add_argument("--accept-limit", action="store_true", help="确认客户已明确接受本项具体限制")
    args = parser.parse_args(argv)
    try:
        flow = Workflow(args.state_dir)
        if args.command == "init":
            result = flow.init({key: getattr(args, key) for key in CHOICES})
        elif args.command == "configure":
            changes = {key: getattr(args, key) for key in CHOICES if getattr(args, key) is not None}
            if not changes:
                raise WorkflowError("configure 至少指定一个设备或目标参数。")
            result = flow.configure(changes)
        elif args.command == "record":
            result = flow.record(args.check_id, args.result, args.note, args.accept_limit)
        else:
            result = flow.view()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (WorkflowError, OSError) as exc:
        message = str(exc) if isinstance(exc, WorkflowError) else "本地进度文件读写失败；检查目录权限，未执行任何网络或设备操作。"
        print(json.dumps({"status": "error", "message": message}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
