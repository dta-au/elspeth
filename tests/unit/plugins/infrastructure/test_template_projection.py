"""A template sees only the fields its node declares (ADR-051, elspeth-5887fb7928 S0 projection).

The row a template renders against is projected in the parent process, before
the context crosses to the render worker (``TemplateRow.project``): a declared
list keeps exactly those fields, ``[]`` keeps the whole row, and an omitted
declaration keeps none. Confidentiality therefore holds by construction, for
every Jinja shape, not only the ones a static analysis can model.

``_LEAK_FORMS`` is every whole-row and field-read escape the S0 review rounds
found against the retired static gate (r1-r3 red-team probes, fix rounds 1-2
and the gate's own positive corpus), deduplicated: 268 forms. Each renders
against a row whose undeclared column carries a sentinel as its value and whose
undeclared key is a sentinel name; none may show either, whatever it renders or
however it fails. ``_MQ_LEAK_FORMS`` are the multi-query forms that reach the
row through ``row.source_row``.
"""

from __future__ import annotations

import pickle

import pytest

from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.schema_contract import FieldContract, PipelineRow, SchemaContract
from elspeth.plugins.infrastructure import templates as template_infrastructure
from elspeth.plugins.infrastructure.templates import (
    ALL_FIELDS,
    AllFields,
    DeclaredFields,
    SandboxedTemplate,
    TemplateError,
    TemplateRow,
    declared_row_projection,
)

_VALUE_SENTINEL = "SENTINEL-P2-VALUE-51c"
_KEY_SENTINEL = "SENTINEL_P2_KEY_51c"
_UNSPELLED = "<a key the template does not spell out>"

_LEAK_FORMS: tuple[str, ...] = (
    "{{ row | dictsort }}",
    "{{ row | items | list }}",
    "{% for k, v in row | items %}{{ v }};{% endfor %}",
    "{% for k in row %}{{ row[k] }};{% endfor %}",
    "{% for k in row %}{{ row.get(k) }};{% endfor %}",
    "{{ row.to_dict() }}",
    "{{ row | dictsort | map('last') | join(',') }}",
    "{{ '%(secret)s' % row }}",
    "{{ row.q }}",
    "{{ row.note }} {{ row | dictsort }}",
    "{{ row.note }}",
    "{{ row.note }} {{ row.secret }}",
    "{{ row.note }} {{ row | default | dictsort }}",
    "{{ row.note }} {{ [row] | first | dictsort }}",
    "{{ row.note }} {{ (row, 1) | first | dictsort }}",
    "{{ row.note }} {{ [row] | last | dictsort }}",
    "{{ row.note }} {{ [row] | random | dictsort }}",
    "{{ row.note }} {{ [row] | max | dictsort }}",
    "{{ row.note }} {{ [row] | sort | first | dictsort }}",
    "{{ row.note }} {{ [row] | reverse | first | dictsort }}",
    "{{ row.note }} {{ [row] | select | first | dictsort }}",
    "{{ row.note }} {{ [row] | selectattr('note') | first | dictsort }}",
    "{{ row.note }} {{ [row] | batch(1) | first | first | dictsort }}",
    "{{ row.note }} {{ ([row] | groupby('note'))[0].list[0] | dictsort }}",
    "{{ row.note }} {{ ([row] | groupby('note'))[0][1][0] | dictsort }}",
    "{{ row.note }} {{ (row if row.note else row) | dictsort }}",
    "{{ row.note }} {{ (row.note and row) | dictsort }}",
    "{{ row.note }} {{ (row.missing_x or row) | dictsort }}",
    "{{ row.note }} {{ row.note | default(row, true) | dictsort }}",
    "{{ row.note }} {{ '%(secret)s' | format(**row) }}",
    "{{ row.note }} {{ dict(a=row).a | dictsort }}",
    "{{ row.note }} {{ cycler(row).next() | dictsort }}",
    "{{ row.note }} {{ cycler(row).current | dictsort }}",
    "{% macro m() %}{{ varargs[0] | dictsort }}{% endmacro %}{{ row.note }} {{ m(row) }}",
    "{% macro m() %}{{ kwargs.r | dictsort }}{% endmacro %}{{ row.note }} {{ m(r=row) }}",
    "{% macro m() %}{{ caller(row) }}{% endmacro %}{{ row.note }} {% call(r) m() %}{{ r | dictsort }}{% endcall %}",
    "{% macro m(r=row) %}{{ r | dictsort }}{% endmacro %}{{ row.note }} {{ m() }}",
    "{% set ns = namespace() %}{% set ns.r = row %}{{ row.note }} {{ ns.r | dictsort }}",
    "{% set r = row %}{% set s = r %}{{ s.note }} {{ s | dictsort }}",
    "{% for k, v in [(1, row)] %}{{ v.note }} {{ v | dictsort }}{% endfor %}",
    "{% for r in [row] recursive %}{{ r.note }} {{ r | dictsort }}{% endfor %}",
    "{{ row.note }} {{ ([row] * 1)[0] | dictsort }}",
    "{{ row.note }} {{ ([row] + [])[0] | dictsort }}",
    "{{ row.note }} {{ [row][-1:] | first | dictsort }}",
    "{{ row.note }} {{ {'a': row}.values() | first | dictsort }}",
    "{{ row.note }} {{ {'a': row}.get('a') | dictsort }}",
    "{{ row.note }} {{ ({'a': row} | dictsort)[0][1] | dictsort }}",
    "{{ row.note }} {{ {'a': row} | dictsort | map('last') | map('dictsort') | list }}",
    "{{ row.note }} {{ ({row.note: row})[row.note] | dictsort }}",
    "{% set r = namespace(x=row).x %}{{ row.note }} {{ r | dictsort }}",
    "{{ row.note }} {{ [[row]] | map('first') | map('dictsort') | list }}",
    "{{ row.note }} {{ [row] | map('default') | map('dictsort') | list }}",
    "{{ row.note }} {{ (row | attr('get'))(row.note) }}",
    "{% set g = row.get %}{{ row.note }} {{ g(row.note) }}",
    "{{ row.note }} {{ (row | attr('get'))('secret') }}",
    "{% set g = row.get %}{{ row.note }} {{ g('secret') }}",
    "{{ row.note }} {{ row.get('secret') }}",
    "{{ row.note }} {{ [row] | map(attribute='secret') | first }}",
    "{{ row.note }} {{ [row] | map(attribute=row.note) | first }}",
    "{{ row.note }} {{ [row] | sum(attribute=row.note) }}",
    "{{ row.note }} {{ [row] | sort(attribute=row.note) | first | dictsort }}",
    "{{ row.note }} {{ [row] | map('attr', row.note) | first }}",
    "{{ row.note }} {{ [row] | selectattr(row.note) | list | length }}",
    "{{ row.note }} {{ row | attr(row.note) }}",
    "{% set k = 'sec' ~ 'ret' %}{{ row.note }} {{ row[k] }}",
    "{{ row.note }} {% for k in row %}{{ row.get(k) }}{% endfor %}",
    "{{ row.note }} {{ row | map('string') | map('attr', 'x') | list }}",
    "{{ row.note }} {{ (row|list)[1] }}",
    "{% for k in row %}{% set v = [row] | map(attribute=k) | first %}{{ v }}{% endfor %}{{ row.note }}",
    "{{ row.note }} {{ row | xmlattr }}",
    "{{ row.note }} {{ row | urlencode }}",
    "{{ row.note }} {{ row | pprint }}",
    "{{ row.note }} {{ row | string }}",
    "{{ row.note }} {{ row | tojson }}",
    "{% macro m() %}{{ varargs[0] | items | list }}{% endmacro %}{{ row.note }} {{ m(row) }}",
    "{% macro m() %}{{ dict(varargs[0]) }}{% endmacro %}{{ row.note }} {{ m(row) }}",
    "{% macro m() %}{{ '%(secret)s' % varargs[0] }}{% endmacro %}{{ row.note }} {{ m(row) }}",
    "{% macro m() %}{{ '%(secret)s' % kwargs.r }}{% endmacro %}{{ row.note }} {{ m(r=row) }}",
    "{% macro m() %}{{ varargs[0] | xmlattr }}{% endmacro %}{{ row.note }} {{ m(row) }}",
    "{% macro m() %}{% for v in varargs %}{{ v | dictsort }}{% endfor %}{% endmacro %}{{ row.note }} {{ m(row) }}",
    "{% macro m(a) %}{{ varargs[0] | dictsort }}{% endmacro %}{{ row.note }} {{ m(1, row) }}",
    "{% macro m(r) %}{{ r | dictsort }}{% endmacro %}{{ row.note }} {{ m(*[row]) }}",
    "{% macro m(r) %}{{ r | dictsort }}{% endmacro %}{{ row.note }} {{ m(**{'r': row}) }}",
    "{% macro m() %}{{ r | dictsort }}{% endmacro %}{% set r = row %}{{ row.note }} {{ m() }}",
    "{% macro m() %}{{ caller() | dictsort }}{% endmacro %}{{ row.note }} {% call m() %}{{ row }}{% endcall %}",
    "{% macro m(r) %}{{ caller(r) }}{% endmacro %}{{ row.note }} {% call(x) m(row) %}{{ x | dictsort }}{% endcall %}",
    "{% macro m() %}{{ caller(*varargs) }}{% endmacro %}{{ row.note }} {% call(x) m(row) %}{{ x | dictsort }}{% endcall %}",
    "{% macro m() %}{{ varargs[0][varargs[1]] }}{% endmacro %}{{ row.note }} {{ m(row, row.note) }}",
    "{% macro m() %}{{ kwargs.r.secret }}{% endmacro %}{{ row.note }} {{ m(r=row) }}",
    "{% macro m() %}{{ varargs[0].secret }}{% endmacro %}{{ row.note }} {{ m(row) }}",
    "{% for r in [row] %}{{ loop.cycle(r) | dictsort }}{% endfor %}{{ row.note }}",
    "{{ row.note }} {{ m | default }}",
    "{% set c = [row, {'r': row}] %}{{ row.note }} {{ c[1].r | dictsort }}",
    "{% set c = [[row]] %}{{ row.note }} {% for v in c %}{% for w in v %}{{ w | dictsort }}{% endfor %}{% endfor %}",
    "{% set c = [{'r': row}] %}{{ row.note }} {% for v in c %}{{ v.r | dictsort }}{% endfor %}",
    "{% set c = [[row]] %}{{ row.note }} {% for v in c %}{{ v[0] | dictsort }}{% endfor %}",
    "{{ row.note }} {% for v in [[row]] %}{% for w in v %}{{ w | dictsort }}{% endfor %}{% endfor %}",
    "{{ row.note }} {% for v in [[[row]]] %}{{ v[0][0] | dictsort }}{% endfor %}",
    "{{ row.note }} {{ [row, {'r': row}][1].r | dictsort }}",
    "{{ row.note }} {{ [1, {'r': row}][1].r | dictsort }}",
    "{% set c = [1, {'r': row}] %}{{ row.note }} {{ c[1].r | dictsort }}",
    "{% set c = {'a': [row]} %}{{ row.note }} {{ c.a[0] | dictsort }}",
    "{% set c = {'a': {'b': row}} %}{{ row.note }} {{ c.a.b | dictsort }}",
    "{% set c = [row, [row]] %}{{ row.note }} {{ c[1][0] | dictsort }}",
    "{% set c = (row, (row,)) %}{{ row.note }} {{ c[1][0] | dictsort }}",
    "{% for a, b in [(row, [row])] %}{{ row.note }} {{ b[0] | dictsort }}{% endfor %}",
    "{% for a, b in [(1, [row])] %}{{ row.note }} {{ b[0] | dictsort }}{% endfor %}",
    "{% for a, b in [(1, {'r': row})] %}{{ row.note }} {{ b.r | dictsort }}{% endfor %}",
    "{% for r in [row, row] %}{{ r.note }} {{ loop.previtem | dictsort }}{% endfor %}",
    "{% for r in [row, row] %}{{ r.note }} {{ loop.nextitem | dictsort }}{% endfor %}",
    "{% for r in [1, row] %}{{ row.note }} {{ loop.nextitem | dictsort if loop.first }}{% endfor %}",
    "{% set l = [] %}{% set _ = l.append(row) %}{{ row.note }} {{ l[0] | dictsort }}",
    "{% set d = {} %}{% set _ = d.update(row) %}{{ row.note }} {{ d | dictsort }}",
    "{{ row.note }} {{ [row].pop() | dictsort }}",
    "{{ row.note }} {{ [row].copy()[0] | dictsort }}",
    "{{ row.note }} {{ {'a': row}.copy().a | dictsort }}",
    "{{ row.note }} {{ {'a': row}.items() | first | last | dictsort }}",
    "{{ row.note }} {{ {'a': row}.pop('a') | dictsort }}",
    "{{ row.note }} {{ {'a': row}.setdefault('a') | dictsort }}",
    "{{ row.note }} {{ row.get('zz_missing', row) | dictsort }}",
    "{% set g = row.get %}{{ row.note }} {{ g('zz_missing', row) | dictsort }}",
    "{{ row.note }} {{ [row] | map('items') | map('list') | list }}",
    "{{ row.note }} {{ [row] | map('xmlattr') | list }}",
    "{{ row.note }} {{ [row] | map('dictsort') | list }}",
    "{{ row.note }} {{ [[row]] | map('first') | map('items') | map('list') | list }}",
    "{% macro m() %}{{ kwargs | dictsort }}{% endmacro %}{{ row.note }} {{ m(**row) }}",
    "{% macro m() %}{{ caller(**row) }}{% endmacro %}{{ row.note }} {% call m() %}{{ kwargs | dictsort }}{% endcall %}",
    "{% macro m(note) %}{{ kwargs | dictsort }}{% endmacro %}{{ row.note }} {{ m(**row) }}",
    "{% macro m(note, secret) %}{{ secret }}{% endmacro %}{{ row.note }} {{ m(**row) }}",
    "{% macro m(note) %}{{ kwargs.secret }}{% endmacro %}{{ row.note }} {{ m(**row) }}",
    "{% macro m() %}{{ varargs[0] | dictsort }}{% endmacro %}{% set f = m %}{{ row.note }} {{ f(row) }}",
    "{% macro m() %}{{ varargs[0] | dictsort }}{% endmacro %}{% macro ap(f, r) %}{{ f(r) }}{% endmacro %}{{ row.note }} {{ ap(m, row) }}",
    "{% macro m(x) %}{{ x | dictsort }}{% endmacro %}{% macro ap(f, r) %}{{ f(r) }}{% endmacro %}{{ row.note }} {{ ap(m, row) }}",
    "{% macro m() %}{{ varargs[0] | dictsort }}{% endmacro %}{{ row.note }} {{ [m] | map('string') | list | length }}{{ ([m] | first)(row) }}",
    "{% macro m() %}{{ varargs[0] | dictsort }}{% endmacro %}{{ row.note }} {{ {'k': m}.k(row) }}",
    "{% macro m() %}{{ varargs[0] | dictsort }}{% endmacro %}{% set ns = namespace(f=m) %}{{ row.note }} {{ ns.f(row) }}",
    "{% macro m() %}{{ varargs[0][0] | dictsort }}{% endmacro %}{{ row.note }} {{ m([row]) }}",
    "{% macro m() %}{{ varargs[0].r | dictsort }}{% endmacro %}{{ row.note }} {{ m({'r': row}) }}",
    "{% macro m() %}{{ kwargs.r[0] | dictsort }}{% endmacro %}{{ row.note }} {{ m(r=[row]) }}",
    "{% macro m() %}{% for v in varargs %}{% for w in v %}{{ w | dictsort }}{% endfor %}{% endfor %}{% endmacro %}{{ row.note }} {{ m([row]) }}",
    "{% macro m() %}{% for k, v in kwargs | items %}{{ v | dictsort }}{% endfor %}{% endmacro %}{{ row.note }} {{ m(r=row) }}",
    "{% macro m() %}{% for v in kwargs.values() %}{{ v | dictsort }}{% endfor %}{% endmacro %}{{ row.note }} {{ m(r=row) }}",
    "{% macro m() %}{{ kwargs.values() | first | dictsort }}{% endmacro %}{{ row.note }} {{ m(r=row) }}",
    "{% macro m() %}{{ varargs | first | dictsort }}{% endmacro %}{{ row.note }} {{ m(row) }}",
    "{% macro m() %}{{ varargs | last | dictsort }}{% endmacro %}{{ row.note }} {{ m(1, row) }}",
    "{% block b %}{{ row | dictsort }}{% endblock %}{{ row.note }}",
    "{{ row.note }}{% set x %}{{ row | dictsort }}{% endset %}{{ x }}",
    "{% for r in [row] recursive %}{{ r.note }}{{ loop([]) }}{{ r | dictsort }}{% endfor %}",
    "{% with r = row %}{{ row.note }} {{ r | dictsort }}{% endwith %}",
    "{% with c = [[row]] %}{{ row.note }} {% for v in c %}{% for w in v %}{{ w | dictsort }}{% endfor %}{% endfor %}{% endwith %}",
    "{{ row.note }} {{ 'y' if row == {'note': '', 'secret': 'SENTINELZQ'} else 'n' }}",
    "{{ row.note }} {{ row.values() }}",
    "{{ row.note }} {{ (row | list | map('string') | list) }}",
    "{% set r = row %}{{ row.note }} {% for k in r %}{{ r.get(k) }}{% endfor %}",
    "{% set c = [[row]] %}{{ row.note }} {% for v in c %}{% for w in v %}{% for k in w %}{{ w[k] }}{% endfor %}{% endfor %}{% endfor %}",
    "{% set c = [[row]] %}{{ row.note }} {% for v in c %}{% for w in v %}{{ w.secret }}{% endfor %}{% endfor %}",
    "{% set c = [row, {'r': row}] %}{{ row.note }} {{ c[1].r.secret }}",
    "{% set d = {'r': row} %}{{ row.note }} {{ d.values() | first | dictsort }}",
    "{% set d = {'r': row} %}{{ row.note }} {% for v in d.values() %}{{ v | dictsort }}{% endfor %}",
    "{% set d = {'r': row} %}{{ row.note }} {{ d.get('r') | dictsort }}",
    "{% set d = {'r': row} %}{{ row.note }} {{ d.items() | first | last | dictsort }}",
    "{% set l = [row] %}{{ row.note }} {% for v in l %}{{ v | dictsort }}{% endfor %}",
    "{% macro m() %}{{ kwargs.get('r') | dictsort }}{% endmacro %}{{ row.note }} {{ m(r=row) }}",
    "{% macro m() %}{% for k, v in kwargs.items() %}{{ v | dictsort }}{% endfor %}{% endmacro %}{{ row.note }} {{ m(r=row) }}",
    "{{ row.note }} {% for v in {'r': row}.values() %}{{ v | dictsort }}{% endfor %}",
    "{% set d = {'r': [row]} %}{{ row.note }} {% for v in d.r %}{{ v | dictsort }}{% endfor %}",
    "{% set d = {'r': {'s': row}} %}{{ row.note }} {% for k in d %}{{ d[k].s | dictsort }}{% endfor %}",
    "{% set c = [[row]] %}{{ row.note }} {{ c[0][0] | dictsort }}",
    "{% set c = [[row]] %}{{ row.note }} {{ c | first | first | dictsort }}",
    "{{ row.note }} {% for v in [[row]] %}{{ v | first | dictsort }}{% endfor %}",
    "{{ row.note }} {% for v in [{'r': row}] %}{{ v.r | dictsort }}{% endfor %}",
    "{{ row.note }} {% for v in [row] %}{{ v | dictsort }}{% endfor %}",
    "{{ row.note }} {% for v in [row] %}{{ v.secret }}{% endfor %}",
    "{{ row.note }} {% for v in [[row]] %}{{ v[0].secret }}{% endfor %}",
    "{{ row | tojson }}",
    "{{ row | urlencode }}",
    "{{ row | xmlattr }}",
    "{{ row | map('upper') | list }}",
    "{{ row | sum }}",
    "{{ dict(row) }}",
    "{{ dict(**row) }}",
    "{{ namespace(**row) }}",
    "{{ namespace(row) }}",
    "{{ '%(x)s' % row }}",
    "{{ '{0[x]}'.format(row) }}",
    "{{ '{r[x]}'.format(r=row) }}",
    "{{ '{0[0][x]}'.format([row]) }}",
    "{{ 'a'.format_map(row) }}",
    "{{ row == {} }}",
    "{{ {} == row }}",
    "{{ row != {} }}",
    "{{ row in [] }}",
    "{{ row is eq({}) }}",
    "{{ x is sameas(row) and row is equalto({}) }}",
    "{{ -row }}",
    "{{ row ~ '' }}",
    "{{ lookup[row] }}",
    "{{ [row] | map('dictsort') | list }}",
    "{{ [row] | map('items') | list }}",
    "{{ [row] | first | dictsort }}",
    "{{ [row][0] | dictsort }}",
    "{{ {'a': row}['a'] | dictsort }}",
    "{{ {'a': row}.a | dictsort }}",
    "{{ {'a': {'b': row}}['a']['b'] | dictsort }}",
    "{{ namespace(r=row).r | dictsort }}",
    "{{ (row or none) | dictsort }}",
    "{{ (row if true else none) | dictsort }}",
    "{{ row | default(none) | dictsort }}",
    "{{ none | default(row) | dictsort }}",
    "{% set r = row %}{{ r | dictsort }}",
    "{% set r = row if true else none %}{{ r | dictsort }}",
    "{% set c = [row] %}{{ c | map('dictsort') | list }}",
    "{% set c = [row] %}{{ c[0] | dictsort }}",
    "{% set c = [row] %}{{ c | first | dictsort }}",
    "{% set d = {'a': row} %}{{ d.a | dictsort }}",
    "{% set d = {'a': {'b': row}} %}{{ d.a.b | dictsort }}",
    "{% set ns = namespace(r=row) %}{{ ns.r | dictsort }}",
    "{% set ns = namespace() %}{% set ns.r = row %}{{ ns.r | dictsort }}",
    "{% for r in [row] %}{{ r | dictsort }}{% endfor %}",
    "{% for k, v in row | items %}{{ v }}{% endfor %}",
    "{% macro m(r) %}{{ r | dictsort }}{% endmacro %}{{ m(row) }}",
    "{% macro m(r) %}{{ r | dictsort }}{% endmacro %}{{ m(r=row) }}",
    "{% macro m() %}{{ kwargs }}{% endmacro %}{{ m(**row) }}",
    "{% macro m(r) %}{{ caller(r) }}{% endmacro %}{% call(x) m(row) %}{{ x | dictsort }}{% endcall %}",
    "{% with r = row %}{{ r | dictsort }}{% endwith %}",
    "{{ row.q.format(row) }}",
    "{{ [row] | selectattr('x') | map('dictsort') | list }}",
    "{{ row[row] }}",
    "{{ [row] | sort | map('dictsort') | list }}",
    "{{ [row] | batch(1) | first | first | dictsort }}",
    "{{ range(10)[row:] }}",
    "{% macro m() %}{{ caller(x=row) }}{% endmacro %}{{ row.note }} {% call(x) m() %}{{ x | dictsort }}{% endcall %}",
    "{% macro m() %}{{ caller() }}{% endmacro %}{{ row.note }} {% call(x=row) m() %}{{ x | dictsort }}{% endcall %}",
    "{% macro m() %}{{ caller(row) }}{% endmacro %}{{ row.note }} {% call m() %}{{ varargs[0] | dictsort }}{% endcall %}",
    "{% macro m() %}{{ caller(r=row) }}{% endmacro %}{{ row.note }} {% call m() %}{{ kwargs.r | dictsort }}{% endcall %}",
    "{% macro m() %}{{ caller(x=row) }}{% endmacro %}{{ row.note }} {% call(x) m() %}{{ x.secret }}{% endcall %}",
    "{% set c = [row] %}{% macro m(a) %}{{ varargs[0] | dictsort }}{% endmacro %}{{ row.note }} {{ m(1, *c) }}",
    "{% set c = [1, row] %}{% macro m(a) %}{{ varargs[0] | dictsort }}{% endmacro %}{{ row.note }} {{ m(*c) }}",
    "{% set c = [row] %}{% macro m() %}{{ varargs[0] | dictsort }}{% endmacro %}{{ row.note }} {{ m(*c) }}",
    "{% set d = {'r': row} %}{% macro m() %}{{ kwargs.r | dictsort }}{% endmacro %}{{ row.note }} {{ m(**d) }}",
    "{% set c = [1, row.get] %}{% macro m(a) %}{{ varargs[0]('secret') }}{% endmacro %}{{ row.note }} {{ m(*c) }}",
    "{% macro m() %}{{ varargs[0]('secret') }}{% endmacro %}{{ row.note }} {{ m(row.get) }}",
    "{% macro m() %}{{ m(r=kwargs) }}{% endmacro %}{{ row.note }} {{ m(r=row) }}",
    "{{ '{0[secret]}'.format(row) }}",
    "{{ '{r[secret]}'.format(r=row) }}",
    "{{ '{0[0][secret]}'.format([row]) }}",
    "{{ {} != row }}",
    "{% macro m() %}{{ kwargs }}{% endmacro %}{{ m(**{'r': row}['r']) }}",
    "{{ namespace(**{'r': row}['r']) }}",
    "{% macro m() %}{{ varargs[0] | dictsort }}{% endmacro %}{{ m(row) }}",
    "{% macro m() %}{{ kwargs.r | dictsort }}{% endmacro %}{{ m(r=row) }}",
    "{% macro m() %}{{ varargs[0] | items | list }}{% endmacro %}{{ m(row) }}",
    "{% macro m() %}{{ varargs[0] | xmlattr }}{% endmacro %}{{ m(row) }}",
    "{% macro m() %}{{ dict(varargs[0]) }}{% endmacro %}{{ m(row) }}",
    "{% macro m() %}{{ '%(x)s' % kwargs.r }}{% endmacro %}{{ m(r=row) }}",
    "{% macro m() %}{% for v in varargs %}{{ v | dictsort }}{% endfor %}{% endmacro %}{{ m(row) }}",
    "{% macro m(a) %}{{ varargs[0] | dictsort }}{% endmacro %}{{ m(1, row) }}",
    "{% macro m() %}{{ caller(*varargs) }}{% endmacro %}{% call(x) m(row) %}{{ x | dictsort }}{% endcall %}",
    "{% macro m() %}{{ caller(**kwargs) }}{% endmacro %}{% call(x) m(x=row) %}{{ x | dictsort }}{% endcall %}",
    "{% macro m() %}{{ caller(x=row) }}{% endmacro %}{% call(x) m() %}{{ x | dictsort }}{% endcall %}",
    "{% macro m() %}{{ caller() }}{% endmacro %}{% call(x=row) m() %}{{ x | dictsort }}{% endcall %}",
    "{% macro m() %}{{ caller(row) }}{% endmacro %}{% call m() %}{{ varargs[0] | dictsort }}{% endcall %}",
    "{% macro m() %}{{ caller(r=row) }}{% endmacro %}{% call m() %}{{ kwargs.r | dictsort }}{% endcall %}",
    "{% macro m() %}{{ varargs[0][0] | dictsort }}{% endmacro %}{{ m([row]) }}",
    "{% set c = [row] %}{% macro m() %}{{ varargs[0] | dictsort }}{% endmacro %}{{ m(*c) }}",
    "{% set d = {'r': row} %}{% macro m() %}{{ kwargs.r | dictsort }}{% endmacro %}{{ m(**d) }}",
    "{% set c = [row] %}{% macro m(a) %}{{ varargs[0] | dictsort }}{% endmacro %}{{ m(1, *c) }}",
    "{% set c = [1, row] %}{% macro m(a) %}{{ varargs[0] | dictsort }}{% endmacro %}{{ m(*c) }}",
)

_MQ_LEAK_FORMS: tuple[str, ...] = (
    "{{ row.source_row | dictsort }}",
    "{% set s = row.source_row %}{{ s | dictsort }}",
    "{{ row['source_row'] | items | list }}",
    "{{ dict(row.source_row) }}",
    "{{ row.source_row[row.k] }}",
    "{{ row.source_row.get(row.k) }}",
    "{{ row.text }} {{ row.source_row | dictsort }}",
    "{% macro m() %}{{ varargs[0] | dictsort }}{% endmacro %}{{ row.text }} {{ m(row.source_row) }}",
    "{% macro m() %}{{ kwargs.r | dictsort }}{% endmacro %}{{ row.text }} {{ m(r=row.source_row) }}",
    "{{ row.text }} {{ row.source_row.secret }}",
    "{% set s = row.source_row %}{{ row.text }} {{ s.secret }}",
    "{% macro m(s) %}{{ s.secret }}{% endmacro %}{{ row.text }} {{ m(row.source_row) }}",
    "{% macro m() %}{{ varargs[0].secret }}{% endmacro %}{{ row.text }} {{ m(row.source_row) }}",
)


def _row(**overrides: object) -> PipelineRow:
    """A row with two declared-shaped fields, an undeclared column and an undeclared extra key, both carrying sentinels."""
    fields = (
        FieldContract("note", "Note", str, True, "declared"),
        FieldContract("amount_usd", "Amount USD", int, True, "declared"),
        FieldContract("secret", "secret", str, True, "declared"),
        FieldContract("optional_x", "Optional X", str, False, "declared", nullable=True),
    )
    contract = SchemaContract(mode="FLEXIBLE", fields=fields, locked=True)
    data: dict[str, object] = {"note": "first", "amount_usd": 5, "secret": _VALUE_SENTINEL, _KEY_SENTINEL: _VALUE_SENTINEL}
    data.update(overrides)
    return PipelineRow(data, contract)


_NOTE_ONLY = DeclaredFields(frozenset({"note"}))
_UNDECLARED_SECRET = "Undeclared field: the template reads 'secret', a field this node does not declare in required_input_fields"


def _outcome(source: str, **context: object) -> str:
    """What a render produces or how it fails, as text. A FrameworkBugError is never caught: it would be ELSPETH's bug."""
    from jinja2 import TemplateSyntaxError

    try:
        return SandboxedTemplate(source).render(**context)
    except (TemplateError, TemplateSyntaxError) as exc:
        return f"{type(exc).__name__}: {exc}"


def _shows_undeclared(text: str) -> bool:
    return _VALUE_SENTINEL in text or _KEY_SENTINEL in text


# ---------------------------------------------------------------------------
# The declaration is read one way
# ---------------------------------------------------------------------------


def test_the_declaration_is_read_one_way() -> None:
    assert declared_row_projection(["note", "amount_usd"]) == DeclaredFields(frozenset({"note", "amount_usd"}))
    assert declared_row_projection([]) is ALL_FIELDS
    # Omitted is never the whole row: it declares nothing.
    assert declared_row_projection(None) == DeclaredFields(frozenset())
    assert type(declared_row_projection(None)) is not AllFields


@pytest.mark.parametrize(
    ("projection", "expected"),
    [
        pytest.param(
            DeclaredFields(frozenset({"note", "amount_usd"})),
            "note,amount_usd,|2|[('amount_usd', 5), ('note', 'first')]",
            id="declared-list",
        ),
        pytest.param(declared_row_projection(None), "|0|[]", id="omitted-is-empty"),
    ],
)
def test_a_row_holds_exactly_its_declared_fields(projection: DeclaredFields, expected: str) -> None:
    source = "{% for k in row %}{{ k }},{% endfor %}|{{ row | length }}|{{ row | dictsort }}"
    assert _outcome(source, row=TemplateRow.project(_row(), projection)) == expected


def test_the_opt_out_holds_the_whole_row() -> None:
    """``[]`` keeps today's whole row: the positive control for every negative below."""
    rendered = _outcome("{{ row | dictsort }}", row=TemplateRow.project(_row(), ALL_FIELDS))
    assert _VALUE_SENTINEL in rendered
    assert _KEY_SENTINEL in rendered


def test_a_declared_field_reads_by_either_spelling() -> None:
    row = TemplateRow.project(_row(), DeclaredFields(frozenset({"amount_usd"})))
    source = "{{ row.amount_usd }}|{{ row['Amount USD'] }}|{{ row.get('Amount USD') }}|{{ 'Amount USD' in row }}|{{ 'amount_usd' in row }}"
    assert _outcome(source, row=row) == "5|5|5|True|True"


def test_a_declared_field_the_row_does_not_carry_is_an_ordinary_absent_key() -> None:
    """``in`` is False, ``get`` returns its default and a lookup is the ordinary undefined — by either spelling."""
    row = TemplateRow.project(_row(), DeclaredFields(frozenset({"note", "optional_x"})))
    source = (
        "{{ 'optional_x' in row }}|{{ 'Optional X' in row }}|{{ row.get('optional_x', 'D') }}|{{ row.get('Optional X') is none }}|"
        "{{ row.optional_x is defined }}|{{ row.optional_x | default('d') }}"
    )
    assert _outcome(source, row=row) == "False|False|D|True|False|d"
    assert _outcome("{{ row.optional_x }}", row=row) == ("TemplateError: Undefined variable: the row has no field 'optional_x'")


@pytest.mark.parametrize(
    "source",
    [
        "{{ 'secret' in row }}",
        "{{ 'secret' not in row }}",
        "{{ row.get('secret', 'D') }}",
        "{{ row.get('secret') }}",
        "{{ row.secret }}",
        "{{ row['secret'] }}",
        "{{ row | attr('secret') }}",
        "{{ row.secret is defined }}",
        "{{ row.secret | default('d') }}",
    ],
)
def test_reading_an_undeclared_field_in_any_form_fails_with_its_own_reason(source: str) -> None:
    """Membership and ``get`` raise too: a template learns nothing about an undeclared field, not even its absence."""
    assert _outcome(source, row=TemplateRow.project(_row(), _NOTE_ONLY)) == f"TemplateError: {_UNDECLARED_SECRET}"


def test_an_undeclared_key_computed_from_the_row_is_not_quoted() -> None:
    """The key may be a row value: it prints only when the template spells it out."""
    row = TemplateRow.project(_row(note=_VALUE_SENTINEL), _NOTE_ONLY)
    # A key inside a format string is not one of the template's literal names either.
    for source in ("{{ row[row.note] }}", "{{ row.note in row }}", "{{ row.get(row.note, 'd') }}", "{{ '{0[secret]}'.format(row) }}"):
        assert _outcome(source, row=row) == (
            f"TemplateError: Undeclared field: the template reads {_UNSPELLED}, a field this node does not declare in required_input_fields"
        )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("{{ dict(row) }}", "{'note': 'first'}"),
        ("{{ dict(**row) }}", "{'note': 'first'}"),
        ("{{ row | items | list }}", "[('note', 'first')]"),
        ("{{ row | dictsort }}", "[('note', 'first')]"),
        ("{% for k, v in row | items %}{{ k }}={{ v }};{% endfor %}", "note=first;"),
        ("{{ row == row }}", "True"),
        ("{{ '%(note)s' % row }}", "first"),
    ],
)
def test_the_mapping_forms_work_on_a_projected_row_and_see_the_declared_fields(source: str, expected: str) -> None:
    """The ``__contains__``/``get`` deviation does not break the Mapping protocol the whole-row forms use."""
    assert _outcome(source, row=TemplateRow.project(_row(), _NOTE_ONLY)) == expected


def test_python_attribute_lookup_of_an_undeclared_name_is_not_an_attribute_error() -> None:
    """jinja's ``attr`` filter asks ``hasattr`` first, which swallows only AttributeError.

    A declared field reads, an absent declared field is an AttributeError
    (``hasattr`` False), and an undeclared name raises the undeclared read, so
    it propagates through ``hasattr`` instead of answering False.
    """
    row = TemplateRow.project(_row(), DeclaredFields(frozenset({"note", "optional_x"})))
    assert TemplateRow.__getattr__(row, "note") == "first"
    with pytest.raises(AttributeError):
        TemplateRow.__getattr__(row, "optional_x")
    with pytest.raises(template_infrastructure._UndeclaredFieldError):
        TemplateRow.__getattr__(row, "secret")


# ---------------------------------------------------------------------------
# What crosses to the render worker
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("projection", [_NOTE_ONLY, declared_row_projection(None)], ids=["declared", "omitted"])
def test_the_transport_bytes_carry_no_undeclared_field(projection: DeclaredFields) -> None:
    context = {"row": TemplateRow.project(_row(), projection), "nested": {"source_row": TemplateRow.project(_row(), projection)}}
    payload = pickle.dumps(template_infrastructure._pack_context_value(context), protocol=5)
    assert not _shows_undeclared(payload.decode("latin-1"))


def test_the_transport_instrument_sees_an_opted_out_row() -> None:
    """Positive control for the byte scan: under ``[]`` the whole row crosses."""
    payload = pickle.dumps(template_infrastructure._pack_context_value({"row": TemplateRow.project(_row(), ALL_FIELDS)}), protocol=5)
    assert _VALUE_SENTINEL.encode() in payload
    assert _KEY_SENTINEL.encode() in payload


def test_the_bytes_a_real_render_sends_to_the_worker_carry_no_undeclared_field(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[bytes] = []
    original = template_infrastructure._run_template_worker

    def record(source: str, payload: bytes, *, value_free: bool = False) -> str:
        sent.append(payload)
        return original(source, payload, value_free=value_free)

    monkeypatch.setattr(template_infrastructure, "_run_template_worker", record)
    assert SandboxedTemplate("{{ row | dictsort }}").render(row=TemplateRow.project(_row(), _NOTE_ONLY)) == "[('note', 'first')]"
    assert len(sent) == 1
    assert not _shows_undeclared(sent[0].decode("latin-1"))
    assert b"first" in sent[0]


def test_an_unprojected_pipeline_row_in_a_context_is_a_framework_bug() -> None:
    """Every PipelineRow is projected by its node's declaration first; one that is not is ELSPETH's bug, top-level or nested."""
    for context in ({"row": _row()}, {"row": {"text": "x", "source_row": _row()}}):
        with pytest.raises(FrameworkBugError) as caught:
            SandboxedTemplate("{{ row }}").render(**context)
        assert str(caught.value) == "A template context carries an unprojected PipelineRow: templates see a TemplateRow.project(...) only"


# ---------------------------------------------------------------------------
# The S0 leak corpus, at runtime
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("projection", [_NOTE_ONLY, declared_row_projection(None)], ids=["declared-note", "omitted"])
@pytest.mark.parametrize("source", _LEAK_FORMS)
def test_no_leak_form_shows_an_undeclared_field(source: str, projection: DeclaredFields) -> None:
    assert not _shows_undeclared(_outcome(source, row=TemplateRow.project(_row(), projection), lookup={}))


@pytest.mark.parametrize(
    "source",
    [
        "{{ row.note }} {{ row | dictsort }}",
        "{% for a, b in [(1, [row])] %}{{ row.note }} {{ b[0] | dictsort }}{% endfor %}",
        "{% set d = {'r': row} %}{{ row.note }} {{ d.values() | first | dictsort }}",
        "{{ row.note }} {% for v in [[row]] %}{{ v[0].secret }}{% endfor %}",
        "{% for r in [1, row] %}{{ row.note }} {{ loop.nextitem | dictsort if loop.first }}{% endfor %}",
        "{% macro m() %}{{ kwargs.r.secret }}{% endmacro %}{{ row.note }} {{ m(r=row) }}",
    ],
)
def test_the_corpus_instrument_detects_a_leak_on_the_whole_row(source: str) -> None:
    """Positive control: the same forms DO show the sentinel when the row is not projected (the ``[]`` opt-out)."""
    assert source in _LEAK_FORMS
    assert _shows_undeclared(_outcome(source, row=TemplateRow.project(_row(), ALL_FIELDS), lookup={}))


@pytest.mark.parametrize("source", _MQ_LEAK_FORMS)
def test_no_multi_query_form_shows_an_undeclared_field_through_source_row(source: str) -> None:
    from types import MappingProxyType

    from elspeth.plugins.transforms.llm.multi_query import QuerySpec

    spec = QuerySpec(name="q", input_fields=MappingProxyType({"text": "note", "body": "note"}))
    context = spec.build_template_context(_row(), _NOTE_ONLY)
    assert not _shows_undeclared(_outcome(source, row=context, lookup={}))


def test_the_multi_query_instrument_detects_a_leak_on_an_opted_out_source_row() -> None:
    from types import MappingProxyType

    from elspeth.plugins.transforms.llm.multi_query import QuerySpec

    spec = QuerySpec(name="q", input_fields=MappingProxyType({"text": "note"}))
    context = spec.build_template_context(_row(), ALL_FIELDS)
    assert _shows_undeclared(_outcome("{{ row.text }} {{ row.source_row | dictsort }}", row=context, lookup={}))
