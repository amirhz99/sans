"""Reply keyboards shared by the bot and the service layer.

SPEC-019: ``Service.apply`` has to attach the share-contact button to a queued
outreach message, and importing ``Bot`` from ``Service`` would create a cycle.
Only ``keyboard`` is sent: ``resize_keyboard`` and ``one_time_keyboard`` are not
documented for the Bale API (SPEC-016).
"""

CONTACT_BUTTON = '📱 ارسال شمارهٔ من'


def contact_keyboard():
    """Return the reply keyboard that asks for the sender's own contact."""
    return {'keyboard': [[{'text': CONTACT_BUTTON, 'request_contact': True}]]}


def hide_keyboard():
    """Return the markup that dismisses the share-contact reply keyboard."""
    return {'remove_keyboard': True}
