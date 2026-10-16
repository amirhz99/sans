"""User-facing views kept separate from domain and transport code."""

from .domain import ResponseLevel


class SessionView:
    def __init__(self, service):
        self.service = service

    @staticmethod
    def _level(value) -> ResponseLevel:
        if isinstance(value, ResponseLevel):
            return value
        try:
            return ResponseLevel(value)
        except (TypeError, ValueError):
            return ResponseLevel.SUMMARY

    def render(self, session, level=ResponseLevel.SUMMARY, admin=False) -> str:
        level = self._level(level)
        fee = f"{session['person_fee']:,} تومان" if session['person_fee'] else 'هنوز اعلام نشده'
        lines = [f"سانس #{session['id']}: {session['title']}",
                 self.service.session_time_display(session),
                 'محل: ' + (session['location'] or 'نامشخص'),
                 f'سهم هر نفر: {fee}']
        if level in (ResponseLevel.DETAILS, ResponseLevel.FULL):
            if session['session_at'] and session['when_text']:
                lines.append('عبارت زمان اعلامی: ' + session['when_text'])
            if session['note']:
                lines.append('توضیح: ' + session['note'])
        if admin and level is ResponseLevel.FULL:
            lines += [f"هزینهٔ زمین: {session['venue_cost'] if session['venue_cost'] is not None else 'نامشخص'} تومان",
                      'وضعیت: ' + ('نهایی' if session['finalized'] else 'در انتظار تکمیل / نهایی‌سازی')]
            for attendance in self.service.db.all(
                    '''SELECT p.name,a.guests FROM attendance a JOIN players p ON p.id=a.player_id
                       WHERE a.session_id=? ORDER BY p.id''', (session['id'],)):
                lines.append(f"• {attendance['name']} + {attendance['guests']} مهمان")
        return '\n'.join(filter(None, lines))

    def for_ref(self, ref=None, level=ResponseLevel.SUMMARY, admin=False) -> str:
        return self.render(self.service.session(ref), level, admin)