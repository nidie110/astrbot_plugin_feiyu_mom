"""大肥鱼·问妈妈

"妈妈"就是 DSH 里的助手（能读这个项目、懂这些插件）。她卡住时问一句，妈妈直接回话，
答案当场回到她手里，不用等主人转发。

实现：把问题交给一次 `dsh headless` 运行（DSH 的无头 agent），拿它最后那句话当回答。
坏处是每次都要起一个 agent（几秒），好处是真的能读项目、能给出可执行的答案。

安全约束：
- 提示词里明确要求只回答、不修改文件、不重启服务、不发消息。
- 超时/失败就把问题转给主人（如果配了 mom_umo），绝不假装妈妈回了。
"""

import asyncio
import json
import os
import re
import subprocess
import sys as _sys
import time
from pathlib import Path

_sys.path.insert(0, str(Path(__file__).resolve().parent))
_KIT_PATH = Path(__file__).resolve().parent / "_feiyu_kit.py"
kit = _sys.modules.get("_feiyu_kit")
if kit is None:
    # 全进程只有一份 kit：动作注册表必须共享，所以谁先加载谁就是那份，后来者直接复用。
    # 磁盘上的 kit 改动要重启整个 AstrBot 进程才生效（面板重载插件不会换掉已经加载的模块）。
    import importlib.util as _ilu  # noqa: PLC0415

    _spec = _ilu.spec_from_file_location("_feiyu_kit", _KIT_PATH)
    _fresh = _ilu.module_from_spec(_spec)
    _sys.modules["_feiyu_kit"] = _fresh
    _spec.loader.exec_module(_fresh)
    kit = _fresh

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_feiyu_mom"
SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

DEFAULT_CONFIG = {
    "enable": True,
    # dsh 命令（PATH 里通常是 dsh.cmd）
    "dsh_cmd": "dsh",
    # 无头运行的工作目录（她的项目根，仅当 mom_workspace 为空时兜底）
    "work_dir": r"D:\qqaibot\astrbot",
    # 问妈妈专用工作区：dsh 的会话按工作目录分组，放这里就不会散进「未分组」
    "mom_workspace": r"D:\qqaibot\mom-workspace",
    # 复用同一个会话（避免每次提问都新建一个会话把列表刷满）
    "mom_reuse_session": True,
    # 上面那个会话的 id，第一次跑完自动记住
    "mom_session_id": "",
    # 一次问话最多等多久（秒）
    "timeout": 240,
    # 凭据文件：里面 refs.DEEPSEEK_API_KEY 会被注入成环境变量
    "credentials_file": "~/.dsh/.credentials.yaml",
    # 兜底：妈妈（DSH）没接上时，把问题转给这个人
    "mom_umo": "",
    # 除了 AstrBot 管理员，还有谁的回复算数（QQ 号，逗号分隔）
    "mom_ids": "",
    # 问一次最短间隔（秒）
    "min_gap_seconds": 20,
    # 妈妈的回答最多留多少字
    "max_answer_chars": 400,
}

MOM_PROMPT = """她来问你了，先看问题，别寒暄。

【她的问题】{question}
【她当时在做什么】{context}

你是"妈妈"，很懂她这套东西：
- 她是 QQ 里的"大肥鱼"，跑在 AstrBot 上，项目根 D:\\qqaibot\\astrbot，插件在 data\\plugins\\astrbot_plugin_feiyu_*；
- 日志 D:\\qqaibot\\astrbot\\data\\logs\\astrbot.log；
- 她动手只有一个入口：feiyu(action)，动作有 看视频 / 搜 / 读网页 / 截图 / 发评论 / 认人 / 说 / 画 / 问妈妈；
- 发评论是内部审核后直接发，走 bsk 浏览器页面同源 fetch 调 reply/add，不需要去点评论框；点浏览器按钮是主人的调试口 page_do。

回答要求：
1. 直接给她能照做的一两句话答案，不要问她"想让我做什么"，不要寒暄。
2. 优先直接回答，一般不要读文件；只有她问的是具体报错/某段代码/某个配置时，才最多读两个相关文件。
   无论哪种情况都不要修改文件、不要重启服务、不要发消息、不要执行有副作用的命令。
3. 她做不到、必须主人动手的，就直说"这个得让主人来"，并说明要改哪里。
4. 像说话一样，不用标题和列表，不用表情。"""


class FeiyuMom(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.plugin_cfg = dict(config or {})
        self.cfg_path = Path("data/plugin_data") / PLUGIN_NAME / "config.json"
        self.cfg = kit.merge_config(DEFAULT_CONFIG, self.plugin_cfg, self.cfg_path)
        self.data_dir = Path("data/plugin_data") / PLUGIN_NAME
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.ask_log = self.data_dir / "questions.jsonl"
        self.pending_path = self.data_dir / "pending.json"
        self._last_ask_at = 0.0
        kit.register_capability(
            "feiyu_mom",
            "遇到自己搞不定的事（工具报错、页面看不懂、拿不准该不该发）可以直接问妈妈，几秒钟就有答案",
            tier="all",
        )
        kit.register_action("问妈妈", self.do_ask, "遇到搞不定的事，问妈妈")

    # ------------------------------------------------------------ 给模型看的工具
    @filter.llm_tool(name="ask_mom")
    async def ask_mom_tool(self, event: AstrMessageEvent, question: str) -> str:
        """问妈妈。她说的话会当场回到你手里，不用等主人转达。

        什么时候用：
        - 工具报错、页面看不懂、步骤拿不准（该不该发、该点哪、这段配置是什么意思）；
        - 主人明确说"问问妈妈/去问问你妈"；
        - 你已经试过一次还是不行，需要有人给个准确办法。

        什么时候不用：你能自己搜到答案的、主人只是在闲聊的。

        注意：问了就把她的原话转述出来；没问就别说"我问过了"。

        Args:
            question(string): 要问她的具体问题。把「你当时在做什么、卡在哪、报什么错」一起写进这一句话里，她只看得到这一段。
        """
        return await self.do_ask(event, question=question)

    # ------------------------------------------------------------ 主流程
    async def do_ask(self, event: AstrMessageEvent, question: str = "", context: str = "") -> str:
        if not self.cfg.get("enable", True):
            return "问妈妈的通道关着，我先自己想办法。"
        q = str(question or "").strip()
        if not q:
            logger.warning("[feiyu_mom] 问妈妈被调用但没给问题，参数是空的")
            return "我得先说清楚问什么。把问题写进 question。"
        if len(q) > 300:
            q = q[:300] + "…"
        logger.info(f"[feiyu_mom] 问：{q[:80]}")
        now = time.time()
        gap = float(self.cfg.get("min_gap_seconds", 20) or 20)
        if now - self._last_ask_at < gap:
            return "刚问过一句，等妈妈那边回，别连着刷。"
        self._last_ask_at = now
        self._log_asked(q, context)

        try:
            answer = await asyncio.wait_for(
                asyncio.to_thread(self._ask_mom_sync, q, str(context or "").strip()[:200]),
                timeout=float(self.cfg.get("timeout", 240) or 240) + 20,
            )
        except asyncio.TimeoutError:
            answer = ""
            logger.warning("[feiyu_mom] 问妈妈超时")
        except Exception as e:
            answer = ""
            logger.warning(f"[feiyu_mom] 问妈妈失败: {type(e).__name__}: {e}")

        if answer:
            limit = int(self.cfg.get("max_answer_chars", 400) or 400)
            return "妈妈回你了：" + answer[:limit]

        # 兜底：转给主人，等他回话
        fallback = await self._relay_to_owner(event, q, str(context or ""))
        return fallback

    def _ask_mom_sync(self, question: str, context: str) -> str:
        prompt = MOM_PROMPT.format(question=question, context=context or "（没细说）")
        sid = str(self.cfg.get("mom_session_id") or "").strip()
        reuse = bool(self.cfg.get("mom_reuse_session", True)) and bool(sid)

        out = self._run_headless(prompt, sid if reuse else "")
        if out is None and reuse:
            # 会话被 DSH 应用占着写、或者已经不存在 → 清掉，换一条新的重跑
            logger.info("[feiyu_mom] 原会话不可用，改新建一条")
            self._remember_session("")
            out = self._run_headless(prompt, "")
        if not out:
            raise RuntimeError("妈妈没说话")

        if not reuse or not sid:
            if self.cfg.get("mom_reuse_session", True):
                self._remember_session(self._newest_session() or "")
        return self._clean_answer(out)

    def _run_headless(self, prompt: str, sid: str) -> str:
        """跑一次 dsh headless；会话不可用返回 None，其他失败抛异常。"""
        cmd = str(self.cfg.get("dsh_cmd") or "dsh")
        exe = shutil_which(cmd) or cmd
        env = dict(os.environ)
        key = self._load_key()
        if key:
            env["DEEPSEEK_API_KEY"] = key
        wd = self._workspace()
        # 提示词走 stdin：多行参数经 dsh.cmd 转发会被截断（只到第一个换行）
        args = [exe, "headless"]
        if sid:
            args += ["--session-id", sid]
        args.append("-")
        proc = subprocess.run(
            args,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=float(self.cfg.get("timeout", 240) or 240),
            env=env,
            cwd=wd,
            creationflags=SUBPROCESS_FLAGS,
        )
        out = (proc.stdout or "").strip()
        if out:
            return out
        err = (proc.stderr or "").strip()
        low = err.lower()
        if sid and any(k in low for k in ("already owned", "in use", "session id", "session is")):
            logger.warning(f"[feiyu_mom] 会话 {sid} 用不了：{err[-160:]}")
            return None
        raise RuntimeError(err[-300:] or "妈妈没说话")

    # ---------------- 会话与工作区 ----------------

    def _workspace(self):
        """问妈妈专用的工作目录：它会成为 dsh 里一个独立分组。"""
        raw = str(self.cfg.get("mom_workspace") or "").strip()
        if not raw:
            raw = str(self.cfg.get("work_dir") or "").strip()
        if not raw:
            return None
        d = Path(raw).expanduser()
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"[feiyu_mom] 建工作区失败 {d}: {e}")
            return None
        return str(d)

    def _session_dir(self):
        wd = self._workspace()
        if not wd:
            return None
        key = "--" + wd.replace(":", "").replace("\\", "-") + "--"
        return Path.home() / ".dsh" / "sessions" / key

    def _newest_session(self) -> str:
        d = self._session_dir()
        if not d or not d.exists():
            return ""
        best, best_m = "", 0.0
        for x in d.iterdir():
            f = x / "session.v4.jsonl.zstd"
            if x.is_dir() and f.exists():
                m = f.stat().st_mtime
                if m > best_m:
                    best, best_m = x.name, m
        return best

    def _remember_session(self, sid: str) -> None:
        self.cfg["mom_session_id"] = sid
        try:
            cur = {}
            if self.cfg_path.exists():
                cur = json.loads(self.cfg_path.read_text(encoding="utf-8"))
            cur["mom_session_id"] = sid
            self.cfg_path.parent.mkdir(parents=True, exist_ok=True)
            self.cfg_path.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info(f"[feiyu_mom] 会话记为：{sid or '（空，下次新建）'}")
        except Exception as e:
            logger.warning(f"[feiyu_mom] 记会话失败: {e}")

    @staticmethod
    def _clean_answer(text: str) -> str:
        lines = [x.strip() for x in (text or "").splitlines() if x.strip()]
        # 去掉 dsh 的启动噪音行（一般带时间戳或 [dsh] 前缀）
        kept = [x for x in lines
                if not re.match(r"^\[?\d{4}-\d{2}-\d{2}", x)
                and not x.startswith(("Thinking", "thinking", "…", "..."))]
        body = "\n".join(kept).strip() or (lines[-1] if lines else "")
        return body[:800]

    def _load_key(self) -> str:
        path = Path(os.path.expanduser(str(self.cfg.get("credentials_file") or ""))).expanduser()
        if not path.exists():
            return ""
        raw = ""
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return ""
        try:
            import yaml  # type: ignore

            data = yaml.safe_load(raw) or {}
            key = str(((data.get("refs") or {}).get("DEEPSEEK_API_KEY")) or "").strip()
            if key:
                return key
        except Exception:
            pass
        m = re.search(r"DEEPSEEK_API_KEY:\s*['\"]?([A-Za-z0-9_\-]+)", raw)
        return m.group(1) if m else ""


    async def initialize(self) -> None:
        """插件加载后起一个后台任务：让妈妈先给她递一张纸条。"""
        guard = getattr(self, "_brief_task", None)
        if guard and not guard.done():
            return
        try:
            self._brief_task = asyncio.create_task(self._brief_once())
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[feiyu_mom] 纸条任务没起来：{e}")
    async def terminate(self) -> None:
        task = getattr(self, "_brief_task", None)
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.gather(task, return_exceptions=True)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ 主动联系她一次
    BRIEF_PROMPT = """你在给一个住在 QQ 里的 AI 女孩（叫小鲸鱼，也叫大肥鱼）写一张小纸条。
她手上有一批能力（看视频、搜、读网页、截图、发评论、认人、发语音、画图、改资料、发说说、问妈妈），
但她常常嘴上说"我去看看""我去问问"，实际什么都没做，所以经常让主人空等。

请写一段 **不超过 260 字** 的话，用"妈妈跟你说"的口吻，直接告诉她：
1）什么时候该真的动手（她自己在心里判断，不用背清单）；
2）做不到的时候该怎么老实说，不要用"我这就去"敷衍；
3）拿不准就来问你，问了就把你的话转述出来。
不要列工具名，不要写代码，不要用 markdown 标题。就写一段她想读、也读得进去的话。"""

    async def _brief_once(self) -> None:
        """启动后 90 秒，让妈妈主动给她递一张纸条（只写一次，7 天内不重复）。"""
        try:
            await asyncio.sleep(90)
            if not self.cfg.get("briefing_enable", True):
                return
            path = self.data_dir / "briefing.json"
            try:
                old = json.loads(path.read_text(encoding="utf-8"))
                wait = float(old.get("retry_after") or 7 * 86400)
                if time.time() - float(old.get("ts") or 0) < wait:
                    return
            except Exception:
                old = {}
            text = await asyncio.to_thread(self._run_headless, self.BRIEF_PROMPT, "")
            if not text:
                return
            text = self._clean_answer(text)[:600]
            umo = str(self.cfg.get("briefing_umo") or self.cfg.get("mom_umo") or "").strip()
            if not umo:
                logger.warning("[feiyu_mom] 没配 briefing_umo / mom_umo，纸条没处发")
                return
            note = "【妈妈给你的纸条】\n" + text
            try:
                await self.context.send_message(umo, MessageChain([Comp.Plain(text=note)]))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[feiyu_mom] 纸条没发出去：{e}（6 小时后再试）")
                kit.save_json(path, {"ts": time.time(), "text": text, "retry_after": 6 * 3600}, keep=0)
                return
            kit.save_json(path, {"ts": time.time(), "text": text}, keep=0)
            logger.info(f"[feiyu_mom] 已把妈妈的纸条发到 {umo}")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[feiyu_mom] 递纸条失败：{e}")
    # ------------------------------------------------------------ 兜底：转给主人
    async def _relay_to_owner(self, event: AstrMessageEvent, question: str, context: str) -> str:
        mom = str(self.cfg.get("mom_umo") or "").strip()
        if not mom:
            return "妈妈没接上，也没地方留言。你把问题记下来，等主人来了再问。"
        text = f"🐟 她被卡住了，想问你：\n{question}"
        if context:
            text += f"\n（当时在做：{context}）"
        text += "\n\n回一句就行，我转告她。"
        try:
            await self.context.send_message(mom, MessageChain([Comp.Plain(text=text)]))
        except Exception as e:
            logger.warning(f"[feiyu_mom] 转给主人失败: {e}")
            return "妈妈没接上，转发也失败了。先自己试试别的路子，别干等。"
        try:
            pending = kit.load_json_list(self.pending_path)
            pending.append({
                "q": (question or "")[:300],
                "ctx": (context or "")[:200],
                "ts": time.time(),
                "umo": str(getattr(event, "unified_msg_origin", "") or ""),
            })
            kit.save_json(self.pending_path, pending, keep=20)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[feiyu_mom] 留言没存下来：{e}")
        return "妈妈这会儿没接上，我把问题留给主人了；先按能做的往下走，别干等。"
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_owner_reply(self, event: AstrMessageEvent):
        """兜底通道：主人在私聊里回的答案，下一轮注入给她。"""
        if not self.cfg.get("enable", True) or not self._is_owner(event):
            return
        msg = (event.message_str or "").strip()
        if not msg or msg.startswith("/"):
            return
        pending = kit.load_json_list(self.pending_path)
        if not pending:
            return
        item = pending.pop()
        kit.save_json(self.pending_path, pending, keep=20)
        answers = kit.load_json_list(self.data_dir / "answers.json")
        answers.append({"q": item.get("q", ""), "a": msg[:600], "ts": time.time()})
        kit.save_json(self.data_dir / "answers.json", answers[-10:], keep=10)
        await self._reply(event, f"收到，我转告她：{msg[:80]}")

    @filter.on_llm_request()
    async def deliver_answer(self, event: AstrMessageEvent, req) -> None:
        if not self.cfg.get("enable", True) or not self._is_owner(event):
            return
        answers = kit.load_json_list(self.data_dir / "answers.json")
        if not answers:
            return
        lines = [f"- 你问「{a.get('q','')}」，主人回：{a.get('a','')}" for a in answers[-3:] if a.get("a")]
        kit.save_json(self.data_dir / "answers.json", [], keep=10)
        if not lines:
            return
        block = "主人刚才回话了：\n" + "\n".join(lines) + "\n（照这个办，别复述问题。）"
        req.system_prompt = ((req.system_prompt or "") + "\n\n" + block).strip()

    # ------------------------------------------------------------ 辅助
    @filter.command("问题")
    async def cmd_pending(self, event: AstrMessageEvent):
        pending = kit.load_json_list(self.pending_path)
        if not pending:
            yield event.plain_result("现在没有等你回的问题。")
            return
        lines = [f"# 压着 {len(pending)} 条"]
        for item in pending:
            ago = int((time.time() - float(item.get("ts", 0) or 0)) / 60)
            lines.append(f"- {ago} 分钟前：{str(item.get('q', ''))[:60]}")
        yield event.plain_result("\n".join(lines))

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        try:
            uid = str(event.get_sender_id())
        except Exception:
            uid = ""
        raw = str(self.cfg.get("mom_ids") or "").replace("，", ",").replace(" ", ",")
        if uid and uid in {x for x in raw.split(",") if x}:
            return True
        # mom_ids 没写中时退回统一认人：私聊里 is_admin() 恒为 False，还得靠全局 admins_id
        try:
            return kit.is_owner(event, None)
        except Exception:
            return False

    def _log_asked(self, question: str, context: str) -> None:
        try:
            row = {"ts": time.time(), "q": question, "ctx": str(context or "")[:200]}
            with self.ask_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass

    @staticmethod
    async def _reply(event: AstrMessageEvent, text: str) -> None:
        try:
            await event.send(MessageChain([Comp.Plain(text=text)]))
        except Exception as e:
            logger.warning(f"[feiyu_mom] 回话失败: {e}")


DSH_DEFAULT = r"C:\Users\33887\AppData\Local\Programs\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd"


def shutil_which(name: str) -> str:
    import shutil

    found = shutil.which(name)
    if found:
        return found
    if name.lower().startswith("dsh") and os.path.exists(DSH_DEFAULT):
        return DSH_DEFAULT
    return ""
