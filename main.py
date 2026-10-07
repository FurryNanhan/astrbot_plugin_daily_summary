import os
import re
from datetime import datetime
from collections import Counter
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from apscheduler.schedulers.asyncio import AsyncIOScheduler


@register("daily_summary", "南寒Han", "统计当日发言排行并生成LLM总结（支持定时推送）", "2.4.1")
class DailySummaryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        # 日志目录配置
        self.log_dir = self.config.get("log_dir", "")
        if not self.log_dir:
            logger.warning("日报插件：未配置日志目录 (log_dir)，请在 WebUI 中填写后再使用！")        
        self.filter_self = self.config.get("filter_self", True)
        self.bot_qq = str(self.config.get("bot_qq", ""))
        if self.filter_self and not self.bot_qq:
            logger.warning("日报插件：已开启过滤机器人消息，但未配置 bot_qq，过滤将不生效！")


        # 定时推送配置
        self.enable_schedule = self.config.get("enable_schedule", True)
        self.schedule_time = self.config.get("schedule_time", "08:00")  # 默认早上8点
        self.target_groups = self.config.get("target_groups", [])  # 要推送的群号列表
        self.use_llm_in_schedule = self.config.get("use_llm_in_schedule", True)  # 定时推送是否带LLM总结

        # 调度器
        self.scheduler = AsyncIOScheduler()
        self._jobs = []

        # 初始化时启动定时任务
        if self.enable_schedule and self.target_groups:
            self._schedule_daily_report()

        logger.info(f"日报插件（定时版）已初始化，日志目录：{self.log_dir}")

    def _schedule_daily_report(self):
        """添加定时日报任务"""
        for job in self._jobs:
            job.remove()
        self._jobs.clear()

        try:
            hour, minute = map(int, self.schedule_time.split(':'))
            if not (0 <= hour < 24 and 0 <= minute < 60):
                raise ValueError
        except Exception:
            logger.error(f"日报插件：定时时间格式错误，已跳过：{self.schedule_time}")
            return

        job = self.scheduler.add_job(
            self._send_daily_report,
            trigger='cron',
            hour=hour,
            minute=minute,
            id="daily_report_job",
            replace_existing=True,
            misfire_grace_time=60
        )
        self._jobs.append(job)
        logger.info(f"日报插件：已添加定时任务，每天 {self.schedule_time} 推送日报到群 {self.target_groups}")

        if not self.scheduler.running:
            self.scheduler.start()

    async def _send_daily_report(self):
        """定时发送日报"""
        logger.info("日报插件：定时推送触发")

        if not self.log_dir:
            logger.warning("日报插件：未配置日志目录 (log_dir)，跳过推送")
            return

        if not self.target_groups:
            logger.warning("日报插件：目标群为空，跳过推送")
            return
            
        # 获取平台适配器
        try:
            platform = self.context.get_platform("aiocqhttp")
            if platform is None:
                logger.error("日报插件：获取 platform 失败")
                return
        except Exception as e:
            logger.error(f"日报插件：获取 platform 异常：{e}")
            return

        bot = getattr(platform, 'bot', None)
        if bot is None:
            bot = getattr(platform, 'client', None)
        if bot is None:
            logger.error("日报插件：无法获取 bot 实例")
            return

        # 对每个目标群生成并发送日报
        for group_id in self.target_groups:
            try:
                # 生成日报内容（使用当前日期）
                today = datetime.now().strftime("%Y-%m-%d")
                log_file = os.path.join(self.log_dir, f"{today}_{group_id}.log")

                if not os.path.exists(log_file):
                    msg = f"📭 今天（{today}）群里还没人说话呢～"
                    await bot.call_action("send_group_msg", group_id=group_id, message=msg)
                    continue

                stats = self._parse_log(log_file)
                if not stats:
                    msg = f"📭 今天（{today}）群里还没人说话呢～"
                    await bot.call_action("send_group_msg", group_id=group_id, message=msg)
                    continue

                rank_text = self._format_stats(stats)

                if self.use_llm_in_schedule:
                    summary = await self._generate_summary(stats, log_file)
                    if summary:
                        msg = f"📊 今日发言排行（{today}）：\n{rank_text}\n\n🧠 AI总结：\n{summary}"
                    else:
                        msg = f"📊 今日发言排行（{today}）：\n{rank_text}\n\n❌ AI总结生成失败"
                else:
                    msg = f"📊 今日发言排行（{today}）：\n{rank_text}"

                await bot.call_action("send_group_msg", group_id=group_id, message=msg)
                logger.info(f"日报插件：已推送到群 {group_id}")

            except Exception as e:
                logger.error(f"日报插件：推送到群 {group_id} 失败：{e}")

    @filter.command_group("daily")
    def daily(self):
        pass

    @daily.command("summary")
    async def daily_summary(self, event: AstrMessageEvent):
        """生成当日发言总结"""
        yield event.plain_result("🧠 正在生成今日总结，请稍候...")
        result = await self._handle_daily(event, llm=True)
        yield event.plain_result(result)

    @daily.command("stats")
    async def daily_stats(self, event: AstrMessageEvent):
        """仅统计排行，不调用LLM"""
        result = await self._handle_daily(event, llm=False)
        yield event.plain_result(result)

    @daily.command("help")
    async def daily_help(self, event: AstrMessageEvent):
        """显示帮助信息"""
        help_text = (
            "📊 日报插件使用说明：\n"
            "发送 [唤醒词]daily summary - 生成今日发言总结（排行+LLM总结）\n"
            "发送 [唤醒词]daily stats   - 仅统计今日发言排行\n"
            "发送 [唤醒词]daily help    - 显示本帮助\n"
            "\n⏰ 定时推送：每天 08:00 自动推送到配置的群"
        )
        yield event.plain_result(help_text)

    async def _handle_daily(self, event: AstrMessageEvent, llm: bool) -> str:
        if not self.log_dir:
            return "❌ 未配置日志目录 (log_dir)，请前往 AstrBot WebUI 插件配置中填写。"
            
        group_id = event.get_group_id()
        if not group_id:
            return "❌ 这个命令只能在群聊中使用嗷～"

        today = datetime.now().strftime("%Y-%m-%d")
        log_file = os.path.join(self.log_dir, f"{today}_{group_id}.log")

        if not os.path.exists(log_file):
            return f"❌ 找不到今天的日志文件：{log_file}\n可能今天还没人说话？"

        stats = self._parse_log(log_file)
        if not stats:
            return "📭 今天还没有人发言呢～"

        rank_text = self._format_stats(stats)

        if not llm:
            return f"📊 今日发言排行（{today}）：\n{rank_text}"

        summary = await self._generate_summary(stats, log_file)
        if summary:
            return f"📊 今日发言排行（{today}）：\n{rank_text}\n\n🧠 AI总结：\n{summary}"
        else:
            return f"📊 今日发言排行（{today}）：\n{rank_text}\n\n❌ AI总结生成失败"

    def _parse_log(self, log_file: str) -> list:
        counter = Counter()
        name_map = {}
        pattern = r'\[.*?\] \[.*?\(.*?\)\] \[(.*?)\((\d+)\)\] (.*)'

        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                for line in f:
                    match = re.search(pattern, line)
                    if match:
                        nickname, qq, content = match.groups()
                        
                        # 过滤机器人自身消息的逻辑
                        if self.filter_self and self.bot_qq and str(qq) == str(self.bot_qq):
                            logger.info(f"日报插件：已成功过滤机器人自身消息：{nickname}({qq})")
                            continue
                            
                        if content.strip() in ["[空消息]", ""]:
                            continue
                        counter[qq] += 1
                        if qq not in name_map or len(nickname) > len(name_map.get(qq, '')):
                            name_map[qq] = nickname
        except Exception as e:
            logger.error(f"解析日志失败：{e}")
            return []

        result = []
        for qq, count in counter.most_common():
            result.append({
                'qq': qq,
                'nickname': name_map.get(qq, '未知'),
                'count': count
            })
        return result

    def _format_stats(self, stats: list) -> str:
        if not stats:
            return "暂无数据"
        lines = []
        for i, item in enumerate(stats, 1):
            if item['nickname'] == item['qq']:
                display_name = item['qq']
            else:
                display_name = f"{item['nickname']} ({item['qq']})"
            lines.append(f"{i}. {display_name}：{item['count']}条")
        total = sum(item['count'] for item in stats)
        lines.append(f"\n📈 今日总消息数：{total}条")
        return "\n".join(lines)

    def _get_provider(self):
        if hasattr(self.context, 'get_using_provider'):
            try:
                provider = self.context.get_using_provider()
                if provider:
                    return provider
            except Exception as e:
                logger.error(f"get_using_provider 失败: {e}")

        if hasattr(self.context, 'provider_manager'):
            mgr = self.context.provider_manager
            if hasattr(mgr, 'get_current_provider'):
                return mgr.get_current_provider()
            if hasattr(mgr, 'current_provider'):
                return mgr.current_provider

        if hasattr(self.context, 'get_provider'):
            return self.context.get_provider()
        if hasattr(self.context, 'provider'):
            return self.context.provider

        return None

    async def _generate_summary(self, stats: list, log_file: str) -> str:
        top5 = stats[:5]
        top5_text = "\n".join([f"{i}. {item['nickname']}（{item['count']}条）" for i, item in enumerate(top5, 1)])

        recent_lines = []
        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                recent_lines = lines[-10:]
        except:
            recent_lines = ["（无法读取最近消息）"]
        recent_text = "".join(recent_lines)

        prompt = f"""
今天是 {datetime.now().strftime('%Y-%m-%d')}，以下是本群今日发言统计：

【发言排行榜】
{top5_text}

【今日总消息数】
{sum(item['count'] for item in stats)} 条

【最近几条消息示例】
{recent_text}

请根据以上信息，生成一段幽默风趣的今日群聊总结，语气轻松活泼，可以适当调侃但不要冒犯。可以提到今天的"话痨王"、活跃氛围、有趣话题等。字数控制在150字以内。
"""

        provider = self._get_provider()
        if not provider:
            logger.error("没有可用的LLM提供商")
            return None

        try:
            messages = [
                {"role": "system", "content": "你是一个幽默风趣的群聊日报撰写员，擅长用轻松的语气总结群聊内容。"},
                {"role": "user", "content": prompt}
            ]
            if hasattr(provider, 'chat'):
                response = await provider.chat(messages=messages)
            elif hasattr(provider, 'text_chat'):
                response = await provider.text_chat(prompt="", contexts=messages)
            else:
                logger.error("LLM提供商没有可用的聊天方法")
                return None

            if response and hasattr(response, 'result_chain') and response.result_chain:
                for comp in response.result_chain.chain:
                    if comp.type == 'Plain' and hasattr(comp, 'text'):
                        return comp.text

            if response:
                if hasattr(response, 'completion') and response.completion:
                    return response.completion
                if hasattr(response, 'content'):
                    return response.content
                if isinstance(response, str):
                    return response

            logger.warning(f"无法从LLM响应中提取纯文本：{type(response)}")
            return None
        except Exception as e:
            logger.error(f"调用LLM失败：{e}")
            return None

    async def terminate(self):
        for job in self._jobs:
            job.remove()
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        logger.info("日报插件已卸载")