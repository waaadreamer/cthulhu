"""中心监控：目前只有整机流量，结构上按“一项一面板”组织，之后的检查（模型指纹、Galaxy API 等）照此添加。

流量取自系统默认路由网卡的内核计数（/proc/net/dev），包含本机所有服务，不只是 Cthulhu。
计费方向是“其他机器从本机拉数据”，即本机出站（tx）。

- 每分钟采样一次，把与上次计数的差值记入当前小时的桶（对象 kind='traffic'，按本地时间小时）。
- 上次计数连同开机 ID 一起持久化：中心重启期间的流量在下次采样时补记，不会丢；
  机器重启后计数归零，此时差值就是开机以来的计数。补记都落在采样所在的小时，这是已知的粗糙之处。
- 小时桶保留 400 天，足够按月对账。
"""
import asyncio
import time
from datetime import datetime, timedelta
from pathlib import Path

SAMPLE_EVERY = 60
KEEP_DAYS = 400


def default_iface():
    """默认路由所在网卡；没有默认路由时取流量最大的非回环网卡。"""
    try:
        for line in Path('/proc/net/route').read_text().splitlines()[1:]:
            parts = line.split()
            if len(parts) > 2 and parts[1] == '00000000':
                return parts[0]
    except OSError:
        pass
    counters = read_all()
    return max((k for k in counters if k != 'lo'), key=lambda k: sum(counters[k]), default=None)


def read_all():
    out = {}
    for line in Path('/proc/net/dev').read_text().splitlines()[2:]:
        name, _, rest = line.partition(':')
        fields = rest.split()
        if len(fields) >= 9:
            out[name.strip()] = (int(fields[0]), int(fields[8]))  # (rx bytes, tx bytes)
    return out


def boot_id():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        return None


def hour_key(ts):
    return datetime.fromtimestamp(ts).strftime('%Y-%m-%dT%H')


class Traffic:
    def __init__(self, store, iface=None, clock=time.time, reader=read_all, boot=boot_id):
        self.store, self.clock, self.reader, self.boot = store, clock, reader, boot
        self.iface = iface or default_iface()
        self.rate = None  # 最近一次采样区间的速率（字节/秒），只在内存里

    def sample(self):
        now, counters = self.clock(), self.reader()
        if self.iface not in counters:
            return None
        rx, tx = counters[self.iface]
        s = self.store
        with s.atomic():
            last = s.find('settings', 'traffic') or {}
            continuous = last.get('iface') == self.iface and last.get('boot') == self.boot() and rx >= last['rx'] and tx >= last['tx']
            if continuous:
                drx, dtx = rx - last['rx'], tx - last['tx']
            elif last.get('iface') == self.iface and last:
                drx, dtx = rx, tx  # 计数归零（机器重启）：开机以来的流量都是新的
            else:
                drx = dtx = 0       # 首次采样或换了网卡：只建立基线，不把历史累计当成本小时流量
            # 速率只在连续计数时有意义：重启后的差值跨越了整段开机时间，不能除以采样间隔。
            if continuous and now > last['at']:
                self.rate = {'rx': drx / (now - last['at']), 'tx': dtx / (now - last['at'])}
            key = hour_key(now)
            bucket = s.find('traffic', key) or {'id': key, 'rx': 0, 'tx': 0}
            bucket['rx'] += drx; bucket['tx'] += dtx
            s.put('traffic', bucket)
            s.put('settings', {'id': 'traffic', 'iface': self.iface, 'boot': self.boot(), 'rx': rx, 'tx': tx, 'at': now,
                               'since': last.get('since') or now})
            cutoff = hour_key(now - KEEP_DAYS * 86400)
            s.db.execute("DELETE FROM objects WHERE kind='traffic' AND id < ?", (cutoff,))
        return drx, dtx

    async def run(self):
        while True:
            try:
                self.sample()
            except OSError:
                pass  # /proc 暂时读不到就跳过这一分钟，下次采样会补上差值
            await asyncio.sleep(SAMPLE_EVERY)

    def summary(self):
        now = self.clock()
        buckets = {b['id']: b for b in self.store.all('traffic')}
        start = datetime.fromtimestamp(now).replace(minute=0, second=0, microsecond=0)
        hours = []
        for i in range(23, -1, -1):
            t = start - timedelta(hours=i)
            b = buckets.get(t.strftime('%Y-%m-%dT%H'), {})
            hours.append({'hour': t.strftime('%Y-%m-%dT%H'), 'at': t.timestamp(), 'rx': b.get('rx', 0), 'tx': b.get('tx', 0)})
        today, month = start.strftime('%Y-%m-%d'), start.strftime('%Y-%m')
        total = lambda prefix, k: sum(b[k] for key, b in buckets.items() if key.startswith(prefix))
        meta = self.store.find('settings', 'traffic') or {}
        return {'iface': self.iface, 'since': meta.get('since'), 'sampled_at': meta.get('at'), 'rate': self.rate,
                'hours': hours,
                'today': {'rx': total(today, 'rx'), 'tx': total(today, 'tx')},
                'month': {'rx': total(month, 'rx'), 'tx': total(month, 'tx'), 'label': month}}


class Monitor:
    """监控面板集合。每个面板是一个返回字典的函数；新增监控项时在这里登记。"""

    def __init__(self, center, iface=None):
        self.traffic = Traffic(center.store, iface)

    def panels(self):
        return {'traffic': self.traffic.summary()}

    async def run(self):
        await self.traffic.run()
