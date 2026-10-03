"""Run with python tests/test_classification.py; no external services required."""
import ast
import re
from pathlib import Path

source = (Path(__file__).resolve().parents[1] / 'bot.py').read_text()
tree = ast.parse(source)
constants = {'BREAK_KW', 'EQUIP_KW', 'BAR_KW', 'BAR_EN_KW'}
nodes = [node for node in tree.body if (
    isinstance(node, ast.FunctionDef) and node.name in {'norm', 'classify'}
) or (
    isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants for t in node.targets)
)]
namespace = {'re': re}
exec(compile(ast.Module(body=nodes, type_ignores=[]), 'bot.py', 'exec'), namespace)
cases = {
    'dolin bitter': 'бар',
    '+ тостер (купи тостер я не могу норм завтракать дома)': 'оборудование',
    'prosseco x3': 'бар',
    'lesbonito (wine) x2': 'бар',
    'PROSECCO x3': 'бар',
    'нужен просеко': 'бар',
    'Dolin vermouth': 'бар',
    'bitters, orgeat, lime juice': 'бар',
    'маракуйя и кокосовое молоко': 'бар',
    'нужны бокалы для вина': 'оборудование',
    'wine glasses x6': 'оборудование',
    'сломался тостер': 'поломка',
    'не работает coffee machine': 'поломка',
    'coffee grinder': 'оборудование',
    'туалетная бумага': 'расходник',
    'office paper': 'расходник',
    'салфетки и перчатки': 'расходник',
    'sir piderson x1': 'расходник',
}
for text, expected in cases.items():
    actual = namespace['classify'](text)
    assert actual == expected, (text, expected, actual)
print(f'Passed {len(cases)} classification cases.')
