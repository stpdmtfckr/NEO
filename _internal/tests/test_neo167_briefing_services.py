from jarvis.runtime.briefing_stream import BriefingDocument

DIVIDER = '━━━━━━━━━━━━━━━━━━━━━━━━'


def test_service_checks_form_one_block_at_the_end():
    doc = BriefingDocument('ru')
    doc.update('greeting', 'Доброе утро')
    doc.update('service.mailru', 'Mail.ru: ...', 'waiting')
    doc.update('service.gmail', 'Gmail: ...', 'waiting')
    doc.update('weather', 'Погода: +5', 'waiting')
    doc.update('service.gmail', 'Gmail: отвечает.')
    doc.update('service.mailru', 'Mail.ru: отвечает.')
    doc.update('weather', 'Погода: +5')
    doc.update('completion', 'Сводка готова.')
    blocks = doc.render().split('\n\n' + DIVIDER + '\n')
    assert blocks == ['Доброе утро', 'Погода: +5', 'Mail.ru: отвечает.\nGmail: отвечает.', 'Сводка готова.']


def test_pending_service_checks_stay_in_the_service_block():
    doc = BriefingDocument('en')
    doc.update('greeting', 'Hello')
    doc.update('service.gmail', 'Gmail: responding.')
    doc.update('service.mailru', 'Mail.ru: checking', 'waiting')
    doc.update('weather', 'Weather: checking', 'waiting')
    blocks = doc.render().split('\n\n' + DIVIDER + '\n')
    assert blocks == ['Hello', 'Weather: checking', 'Gmail: responding.\nMail.ru: checking']
