# -*- coding: utf-8 -*-
"""离线演示数据（开源版）。

设计原则：
1. **完全虚构**——人物、任务、习惯、关系、健康趋势均为合成数据，不来自
   任何真实用户的个人信息改名；
2. **无外部依赖**——不访问 TickTick / 健康设备 / Obsidian / LLM；
3. **确定性生成**——按当前日期相对推算（今天/昨天/上周…），任意一天打开
   都是一份"活的"演示：今天有到期任务、昨天有睡眠、本周有打卡记录；
4. **可重置**——所有演示写入都落在 DASH_DATA_DIR 内，删除该目录即恢复初始。

数据规模（按项目文档要求）：
- 3 个角色（含使命/KR/习惯/行动/复盘）
- 10 条任务（覆盖四象限、逾期/今日/本周、优先级、大石头）
- 5 个习惯（Boolean + Real 混合，84 天热力图数据）
- 4 条关系记录 + 2 条倾听笔记 + 3 条影响圈条目
- 7 天健康趋势（睡眠/步数/心率变异性/锻炼分钟）
- 1 份本周复盘草稿
"""

import os
import random
from datetime import date, timedelta

DEMO_DIR_NAME = "demo"


def demo_dir(data_dir):
    return os.path.join(data_dir, DEMO_DIR_NAME)


# ─── 虚构人物设定（合成，无真实指向）──────────────────────────────

DEMO_ROLES = [
    {
        "id": "learner",
        "name": "深度学习者",
        "emoji": "📚",
        "mission": "每季度掌握一个能改变我工作方式的知识领域，并把它讲给别人听。",
        "goal": "本季度：完成统计思维课程，用真实数据做一个小型分析项目。",
        "key_results": [
            {"text": "完成课程全部 12 讲并做笔记", "done": False},
            {"text": "用公开数据集完成一个分析小项目", "done": False},
            {"text": "给团队做一次 20 分钟的内部分享", "done": True},
        ],
        "actions": [
            {"text": "看完第 9 讲：假设检验", "done": False, "pushed": False, "taskId": ""},
            {"text": "整理前 8 讲笔记成知识卡", "done": True, "pushed": False, "taskId": ""},
        ],
        "insights": ["学习输入稳定，但输出（分享/项目）节奏偏慢——输入:输出接近 5:1。"],
    },
    {
        "id": "healthkeeper",
        "name": "健康守门人",
        "emoji": "🏃",
        "mission": "把身体当作长期资产经营：睡眠优先，运动其次，饮食顺势。",
        "goal": "本季度：平均睡眠 7.5 小时以上，每周跑步 3 次。",
        "key_results": [
            {"text": "连续 4 周平均睡眠 ≥7.5h", "done": False},
            {"text": "每周跑步 ≥3 次，单次 ≥5km", "done": False},
        ],
        "actions": [
            {"text": "周三傍晚跑步 5km", "done": True, "pushed": False, "taskId": ""},
            {"text": "22:30 前放下手机上床", "done": False, "pushed": False, "taskId": ""},
        ],
        "insights": ["睡眠负债在上周三达到峰值，与当天的复盘缺失高度相关。"],
    },
    {
        "id": "connector",
        "name": "关系园丁",
        "emoji": "🌳",
        "mission": "重要关系不靠心情维护，靠系统：每周一次有质量的联系。",
        "goal": "本季度：不遗漏任何一次重要的家人朋友事件。",
        "key_results": [
            {"text": "每周主动联系 2 位重要的人", "done": False},
            {"text": "给每位密友建一份「倾听笔记」", "done": False},
        ],
        "actions": [
            {"text": "给老同学打电话聊近况", "done": False, "pushed": False, "taskId": ""},
        ],
        "insights": ["情感账户提现集中在「临时爽约」类事件；存款多来自小而具体的关心。"],
    },
]


def _demo_tasks(today):
    """10 条任务：相对今天推算 due（逾期/今日/明日/本周五/无日期）。"""
    fri = today + timedelta(days=(4 - today.weekday()) % 7 or 7)
    return [
        {"id": "demo-t1", "title": "整理本月支出并更新预算表", "pid": "demo-p1", "projectId": "demo-p1",
         "content": "角色：理财学徒\n象限：q2\n大石头", "due": today.isoformat(), "priority": 5,
         "status": 0, "tags": ["大石头"]},
        {"id": "demo-t2", "title": "回复导师的邮件（拖了三天）", "pid": "demo-p1", "projectId": "demo-p1",
         "content": "角色：深度学习者\n象限：q1", "due": (today - timedelta(days=1)).isoformat(),
         "priority": 5, "status": 0, "tags": []},
        {"id": "demo-t3", "title": "跑步 5 公里", "pid": "demo-p1", "projectId": "demo-p1",
         "content": "角色：健康守门人\n象限：q2", "due": today.isoformat(), "priority": 3,
         "status": 0, "tags": []},
        {"id": "demo-t4", "title": "预约年度体检", "pid": "demo-p1", "projectId": "demo-p1",
         "content": "角色：健康守门人\n象限：q2", "due": (today + timedelta(days=1)).isoformat(),
         "priority": 3, "status": 0, "tags": []},
        {"id": "demo-t5", "title": "给课程项目找一份公开数据集", "pid": "demo-p1", "projectId": "demo-p1",
         "content": "角色：深度学习者\n象限：q2", "due": fri.isoformat(), "priority": 4,
         "status": 0, "tags": []},
        {"id": "demo-t6", "title": "周五前交内部分享的提纲", "pid": "demo-p1", "projectId": "demo-p1",
         "content": "角色：深度学习者\n象限：q1", "due": fri.isoformat(), "priority": 5,
         "status": 0, "tags": []},
        {"id": "demo-t7", "title": "给奶奶打电话", "pid": "demo-p1", "projectId": "demo-p1",
         "content": "角色：关系园丁\n象限：q2", "due": None, "priority": 3,
         "status": 0, "tags": []},
        {"id": "demo-t8", "title": "整理书桌和数字文件夹", "pid": "demo-p1", "projectId": "demo-p1",
         "content": "象限：q3", "due": None, "priority": 1, "status": 0, "tags": []},
        {"id": "demo-t9", "title": "随手刷手机的时间超过 2 小时", "pid": "demo-p1", "projectId": "demo-p1",
         "content": "象限：q4（识别并减少）", "due": None, "priority": 0, "status": 0, "tags": []},
        {"id": "demo-t10", "title": "把上周课程笔记整理成三张知识卡", "pid": "demo-p1", "projectId": "demo-p1",
         "content": "角色：深度学习者\n象限：q2", "due": None, "priority": 3,
         "status": 2, "completedTime": (today - timedelta(days=1)).isoformat() + "T20:00:00+0000", "tags": []},
    ]


def _demo_habits(today):
    """5 个习惯 + 84 天确定性好率模式打卡（seed 稳定）。"""
    rng = random.Random(42)  # 固定种子：每次打开演示数据一致
    defs = [
        {"id": "demo-h1", "name": "23:00 前入睡", "type": "Boolean", "goal": 1, "unit": "次",
         "targetDays": 21, "dimension": "身体", "role": "健康守门人", "rate": 0.72},
        {"id": "demo-h2", "name": "晨间日记", "type": "Boolean", "goal": 1, "unit": "次",
         "targetDays": 21, "dimension": "心智", "role": "深度学习者", "rate": 0.55},
        {"id": "demo-h3", "name": "跑步（公里）", "type": "Real", "goal": 15, "unit": "公里",
         "targetDays": 21, "dimension": "身体", "role": "健康守门人", "rate": 0.45, "value": 5.2},
        {"id": "demo-h4", "name": "深度阅读 30 分钟", "type": "Boolean", "goal": 1, "unit": "次",
         "targetDays": 21, "dimension": "心智", "role": "深度学习者", "rate": 0.80},
        {"id": "demo-h5", "name": "主动联系一位重要的人", "type": "Boolean", "goal": 2, "unit": "次",
         "targetDays": 21, "dimension": "关系", "role": "关系园丁", "rate": 0.38},
    ]
    days84 = [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(83, -1, -1)]
    for h in defs:
        daily = {}
        streak = 0
        for d in days84:
            hit = rng.random() < h["rate"]
            if hit:
                daily[d] = h.get("value", 1) if h["type"] == "Real" else 1
            else:
                daily[d] = 0 if h["type"] == "Real" else 0
        # 连续打卡（从今天往回数）
        for d in reversed(days84):
            if daily.get(d, 0) > 0:
                streak += 1
            else:
                break
        h["daily"] = daily
        h["streak"] = streak
        h["weekChecked"] = sum(1 for d in days84[-7:] if daily.get(d, 0) > 0)
        h["weekTotal"] = h["goal"] * 7 if h["type"] == "Real" else 7
        h["checked"] = daily.get(days84[-1], 0) > 0
        h["totalCheckIns"] = sum(1 for v in daily.values() if v > 0)
    return defs


def _demo_relations():
    """4 条情感账户记录（虚构人物）。"""
    base = date.today()
    return [
        {"id": "demo-r1", "who": "老周（大学室友）", "type": "存款", "amount": 20,
         "reason": "他换城市，主动帮忙对接了落脚信息", "role": "关系园丁",
         "date": (base - timedelta(days=2)).isoformat(), "deleted": False},
        {"id": "demo-r2", "who": "母亲", "type": "存款", "amount": 15,
         "reason": "生日前提前订了她提过一次的点心", "role": "关系园丁",
         "date": (base - timedelta(days=5)).isoformat(), "deleted": False},
        {"id": "demo-r3", "who": "同事小林", "type": "取款", "amount": 10,
         "reason": "临时把锅甩给了她赶工", "role": "关系园丁",
         "date": (base - timedelta(days=1)).isoformat(), "deleted": False},
        {"id": "demo-r4", "who": "老周（大学室友）", "type": "存款", "amount": 5,
         "reason": " quarterly 一次的电话粥", "role": "关系园丁",
         "date": (base - timedelta(days=12)).isoformat(), "deleted": False},
    ]


def _demo_listening():
    base = date.today()
    return [
        {"id": "demo-l1", "who": "同事小林", "feeling": "连续加班后的疲惫和委屈",
         "restate": "你不是抱怨工作量，是希望排期时被提前商量",
         "third": "下次排期会上直接把可商量的时间窗摆出来",
         "date": (base - timedelta(days=1)).isoformat(), "deleted": False},
        {"id": "demo-l2", "who": "母亲", "feeling": "惦记，但怕打扰你",
         "restate": "她想多知道你的近况，又不希望你分心",
         "third": "固定每周日晚主动打一通，比偶尔的长时间通话更有效",
         "date": (base - timedelta(days=6)).isoformat(), "deleted": False},
    ]


def _demo_proactive():
    base = date.today()
    return {
        "concerns": [
            {"id": "demo-pc1", "circle": "influence", "text": "把「马上回邮件」的默认延迟改成 2 小时",
             "converted": True, "date": (base - timedelta(days=3)).isoformat(), "deleted": False},
            {"id": "demo-pc2", "circle": "concern", "text": "行业整体的岗位收缩传闻",
             "converted": False, "date": (base - timedelta(days=2)).isoformat(), "deleted": False},
            {"id": "demo-pc3", "circle": "influence", "text": "每周留出一个上午的深度工作块",
             "converted": False, "date": (base - timedelta(days=1)).isoformat(), "deleted": False},
        ],
        "logs": [
            {"id": "demo-pl1", "from": "我没办法拒绝加班", "to": "我可以先问清优先级再答复",
             "date": (base - timedelta(days=2)).isoformat()},
            {"id": "demo-pl2", "from": "都是环境的问题", "to": "环境里我还能动的一格是什么",
             "date": (base - timedelta(days=4)).isoformat()},
        ],
        "checkins": {},
    }


def _demo_health(today):
    """7 天健康趋势：确定性波形（无真实数据）。"""
    import math
    sleep, steps, hrv, exercise, readiness = {}, {}, {}, {}, {}
    for i in range(6, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        phase = (today.timetuple().tm_yday + i) % 7
        sleep[d] = round(6.6 + 1.1 * math.sin(phase / 7 * 2 * math.pi) + (0.2 if i == 0 else 0), 2)
        steps[d] = int(7200 + 3600 * math.sin(phase / 7 * 2 * math.pi + 1) + (900 if i % 2 else -600))
        hrv[d] = int(48 + 12 * math.sin(phase / 7 * 2 * math.pi + 2))
        exercise[d] = int(28 + 22 * math.sin(phase / 7 * 2 * math.pi + 3)) if i != 6 else 0
    # 产出形状与 _health_from_vps 契约一致：各指标为 [{date, value}] 数组
    def _arr(series):
        return [{"date": d, "value": v} for d, v in series.items()]
    empty_extra = {k: [] for k in ("heart", "energy", "mindful", "rhr", "spo2",
                                    "resp", "dist", "workouts", "wrist", "stand", "effort")}
    return {
        "source": "demo",
        "days": [d for d, _ in sleep.items()],
        "units": {"sleep": "h", "steps": "步", "hrv": "ms", "exercise": "min"},
        "sleep": _arr(sleep), "steps": _arr(steps), "hrv": _arr(hrv),
        "exercise": _arr(exercise),
        "workout_quality": {"count": 0},
        "fetched_at": today.isoformat(),
        **empty_extra,
    }


def _demo_week_plan(today):
    fri = today + timedelta(days=(4 - today.weekday()) % 7 or 7)
    return {
        "week": today.strftime("%G-W%V"),
        "big_rocks": [
            {"id": "demo-wr1", "title": "完成课程第 9-10 讲", "why": "项目分析依赖这两讲的方法",
             "role": "深度学习者", "dueDate": fri.isoformat(), "taskId": "demo-t5"},
        ],
        "revision": 2,
    }


def _demo_review(today):
    wk = today.strftime("%G-W%V")
    return {
        "week": wk,
        "text": (
            "## 本周做得好的\n"
            "- 深度阅读习惯保持住了（6/7 天），晨间日记有两天断档但都补了。\n"
            "- 周三的跑步在加班日仍然完成了，说明「先跑再吃晚饭」这个顺序有效。\n\n"
            "## 本周欠账\n"
            "- 情感账户出现一笔取款（把锅甩给同事），当周没有做存款对冲。\n"
            "- 影响圈两条「转任务」只转了一条，另一条还在 concerns 里躺着。\n\n"
            "## 下周只改一件事\n"
            "- 把「主动联系」从心愿改成制度：周日晚 20:00 固定电话时间。"
        ),
    }


# ─── 对外装配接口 ─────────────────────────────────────────────

def build_demo_payload(today=None):
    """一次性产出全部演示域（供 /api 注入层使用）。"""
    today = today or date.today()
    return {
        "roles": DEMO_ROLES,
        "tasks": _demo_tasks(today),
        "habits": _demo_habits(today),
        "relations": _demo_relations(),
        "listening": _demo_listening(),
        "proactive": _demo_proactive(),
        "health": _demo_health(today),
        "week_plan": _demo_week_plan(today),
        "review": _demo_review(today),
    }
