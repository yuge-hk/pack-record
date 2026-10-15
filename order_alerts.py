"""Versioned order input contract for future XLSX, ERP and userscript adapters."""
from datetime import datetime, timezone
import json

from recording_library import utc_now


class OrderAlerts:
    def __init__(self, library, settings, notify):
        self.library, self.settings, self.notify = library, settings, notify
        self.active = None
        self.announced = set()

    def upsert(self, rows):
        if not isinstance(rows, list) or len(rows) > 500:
            raise ValueError('每次最多提交 500 条订单')
        normalized = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('订单必须为对象')
            barcode = row.get('tracking_number', '')
            if not isinstance(barcode, str) or not 1 <= len(barcode.strip()) <= 256:
                raise ValueError('订单必须包含 tracking_number 单号')
            quantity = row.get('quantity', 1)
            if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 10000:
                raise ValueError('商品总件数必须为 1～10000 的整数')
            value = dict(tracking_number=barcode.strip(), quantity=quantity)
            for name in ('buyer_remark', 'seller_remark', 'product_summary', 'source'):
                text = row.get(name, '')
                if not isinstance(text, str) or len(text) > 1000:
                    raise ValueError(f'{name} 必须为不超过 1000 字的文本')
                value[name] = text.strip()
            status = row.get('refund_status', 'unknown')
            if status not in ('unknown', 'none', 'pending', 'refunded'):
                raise ValueError('退款状态必须为 unknown/none/pending/refunded')
            value['refund_status'] = status
            stamp = row.get('updated_at', utc_now())
            try:
                parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    raise ValueError('必须包含时区')
                if (parsed - datetime.now(timezone.utc)).total_seconds() > 300:
                    raise ValueError('订单时间不能在未来')
            except (ValueError, TypeError, AttributeError):
                raise ValueError('updated_at 必须为带时区的 ISO 时间且不能在未来') from None
            value['updated_at'] = parsed.astimezone(timezone.utc).isoformat()
            normalized.append(value)
        with self.library.connect() as db:
            for value in normalized:
                existing = db.execute('SELECT updated_at FROM orders WHERE barcode=?', (value['tracking_number'],)).fetchone()
                if existing and existing[0] > value['updated_at']:
                    continue
                db.execute('INSERT OR REPLACE INTO orders VALUES(?,?,?)',
                           (value['tracking_number'], json.dumps(value, ensure_ascii=False), value['updated_at']))
        if self.active:
            self.announce(self.active[0], self.active[1])
        return dict(imported=len(normalized))

    def lookup(self, tracking):
        with self.library.connect() as db:
            row = db.execute('SELECT payload FROM orders WHERE barcode=?', (tracking,)).fetchone()
        return json.loads(row[0]) if row else None

    def announce(self, tracking, session_id):
        self.active = (tracking, session_id)
        settings = self.settings()
        if not settings.get('speech_enabled', True):
            return
        order = self.lookup(tracking)
        if not order:
            return
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(order['updated_at'])).total_seconds()
        if age > settings.get('order_valid_hours', 24) * 3600:
            return
        signature = (session_id, json.dumps(order, sort_keys=True))
        if signature in self.announced:
            return
        self.announced.add(signature)
        if len(self.announced) > 1000:
            self.announced = {signature}
        parts = []
        warning = settings.get('speech_refund', True) and order['refund_status'] in ('pending', 'refunded')
        if warning:
            parts.append('注意，订单正在退款，请核对后再发货' if order['refund_status'] == 'pending' else '注意，订单已退款，请暂停发货并核对')
        if settings.get('speech_quantity', True) and order['quantity'] > 1:
            parts.append(f"注意，此单共 {order['quantity']} 件商品，请核对数量")
        if settings.get('speech_remarks', True):
            for key, label in (('buyer_remark', '买家留言'), ('seller_remark', '卖家备注'), ('product_summary', '商品')):
                if order[key]:
                    parts.append(label + '，' + order[key][:160])
        if parts:
            self.notify('announcement', dict(text='。'.join(parts), warning=warning,
                                            tracking_number=tracking, time=utc_now(),
                                            station_id=settings['station_id']))
