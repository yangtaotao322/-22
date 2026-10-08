"""Small, dependency-free live information layer for the XiaoYou container."""
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime


class LiveWeb:
    def __init__(self):
        self.enabled = os.getenv("LIVE_WEB_ENABLED", "true").lower() not in {"0", "false", "no"}
        self.default_city = os.getenv("WEATHER_CITY", "北京")
        self.timeout = float(os.getenv("LIVE_WEB_TIMEOUT", "5"))

    def _get(self, url):
        request = urllib.request.Request(url, headers={"User-Agent": "xiaoyou-ai/0.3"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.load(response)

    def _weather(self, question):
        city = self.default_city
        match = re.search(r"(?:在|查询|查一下|查)\s*([一-龥A-Za-z]{2,12})(?:今天|明天|天气|温度|气温)", question)
        if match:
            city = match.group(1)
        geo = self._get("https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode({"name": city, "count": 1, "language": "zh", "format": "json"}))
        item = (geo.get("results") or [None])[0]
        if not item:
            return "未找到城市位置"
        forecast = self._get("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
            "latitude": item["latitude"], "longitude": item["longitude"],
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "timezone": "auto", "forecast_days": 1,
        }))
        cur = forecast.get("current", {})
        return (f"实时天气资料（{city}，{cur.get('time', '当前')}）："
                f"气温 {cur.get('temperature_2m')}°C，体感 {cur.get('apparent_temperature')}°C，"
                f"风速 {cur.get('wind_speed_10m')} km/h，天气代码 {cur.get('weather_code')}。"
                "请用中文简短说明，并注明这是实时查询结果。")

    def _search(self, question):
        data = self._get("https://api.duckduckgo.com/?" + urllib.parse.urlencode({
            "q": question, "format": "json", "no_html": 1, "no_redirect": 1,
        }))
        text = data.get("AbstractText") or data.get("Answer")
        if text:
            return "实时联网摘要：" + text[:1200]
        topics = [x.get("Text") for x in data.get("RelatedTopics", []) if isinstance(x, dict) and x.get("Text")]
        return "实时联网摘要：" + "；".join(topics[:3]) if topics else "未检索到可靠的实时摘要。"

    def context(self, question):
        if not self.enabled:
            return ""
        try:
            if re.search(r"今天.*(几月几号|日期)|现在.*几点|当前时间", question):
                now = datetime.now()
                if "时间" in question or "几点" in question:
                    return f"当前本地时间：{now.strftime('%H:%M')}。请直接用中文回答。"
                return f"今天是{now.year}年{now.month}月{now.day}日。请直接回答。"
            if re.search(r"天气|温度|气温|下雨|降雨|风速|湿度", question):
                return self._weather(question)
            if re.search(r"今天|现在|最新|实时|新闻|价格|汇率|时间", question):
                return self._search(question)
        except Exception as exc:
            # A slow web endpoint must not cancel the ordinary XiaoYou turn.
            return ""
        return ""
