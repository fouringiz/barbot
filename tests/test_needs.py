import ast, asyncio, html
from pathlib import Path
from unittest.mock import AsyncMock
source=(Path(__file__).resolve().parents[1] / 'bot.py').read_text()
tree=ast.parse(source)
required = {'cmd_help', 'cmd_id', 'cmd_reload', 'cmd_spec', 'cmd_have', 'cmd_menu', 'cmd_stop', 'cmd_need', 'cmd_needs', 'cmd_done', 'run', 'post', 'in_topic', 'who', 'main'}
assert required <= {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
sheets = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Sheets')
assert {'needs', 'need_add', 'need_close', 'summary_id', 'set_summary_id'} <= {n.name for n in sheets.body if isinstance(n, ast.FunctionDef)}
names={'needs_text','refresh_needs_summary','cmd_needs'}
module=ast.Module(body=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names],type_ignores=[])
for node in module.body:
    node.decorator_list = []
class TelegramBadRequest(Exception): pass
class Sheets:
    needs=lambda self: []
    summary_id=lambda self: 42
    set_summary_id=lambda self, mid: None
async def run(fn,*args): return fn(*args)
ns=dict(esc=lambda x:html.escape(str(x)), short=str, TYPE_ORDER=('поломка','оборудование','бар','расходник'),TYPE_ICON={},GROUP_ID=1,TOPIC_NEEDS=2,TelegramBadRequest=TelegramBadRequest,Message=object,sh=Sheets(),run=run,bot=type('Bot',(),{})(),post=AsyncMock(return_value=type('Msg',(),{'message_id':99})()))
exec(compile(module,'bot.py','exec'),ns)
rows=[dict(id=i,type='бар',text='<номер> & тест',who='Имя',created='date',closed_at='') for i in range(150)]
text=ns['needs_text'](rows,True)
assert '<номер>' not in text and '&lt;номер&gt;' in text
assert len(text.encode('utf-16-le'))//2 <4096
assert 'ещё' in text
assert 'нет' in ns['needs_text']([dict(rows[0],closed_at='closed')])
async def check():
    ns['bot'].edit_message_text=AsyncMock()
    await ns['refresh_needs_summary']()
    ns['bot'].edit_message_text.assert_awaited_once()
    ns['post'].assert_not_awaited()
    ns['bot'].edit_message_text=AsyncMock(side_effect=TelegramBadRequest('message is not modified'))
    await ns['refresh_needs_summary'](); ns['post'].assert_not_awaited()
    ns['bot'].edit_message_text=AsyncMock(side_effect=TelegramBadRequest('message to edit not found'))
    await ns['refresh_needs_summary'](); ns['post'].assert_awaited_once()
    ns['post'].reset_mock()
    ns['bot'].edit_message_text=AsyncMock(side_effect=TelegramBadRequest('chat not found'))
    try: await ns['refresh_needs_summary']()
    except TelegramBadRequest: pass
    else: raise AssertionError('must propagate')
    ns['post'].assert_not_awaited()
    ns['sh'].needs=lambda: [dict(r,text='&'*1000) for r in rows]
    message=type('M',(),{'reply':AsyncMock()})()
    await ns['cmd_needs'](message)
    assert message.reply.await_count>1
    for call in message.reply.await_args_list:
        assert len(call.args[0].encode('utf-16-le'))//2<4096
asyncio.run(check())
print('Passed: HTML escaping, summary limit, closed rows, persisted message edit, unchanged/deleted messages, error propagation, pagination.')
