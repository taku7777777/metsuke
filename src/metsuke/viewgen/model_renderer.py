"""Render immutable view-model primitives with the established HTML renderer."""

from __future__ import annotations

from metsuke.viewmodel import common

from . import render


def _value(value):
    if isinstance(value, common.Node):
        return _node(value)
    if isinstance(value, common.FrozenMap):
        return {key: _value(item) for key, item in value.items}
    if isinstance(value, common.Column):
        return render.Column(value.label, value.cls, value.sortable, value.sort_dir)
    if isinstance(value, common.Cell):
        return render.Cell(
            value.text,
            value.cls,
            value.sort,
            value.title,
            value.bar,
            None if value.content is None else _node(value.content),
            value.clip,
            value.dot,
            value.warn,
        )
    if isinstance(value, common.Row):
        return render.Row(tuple(_value(cell) for cell in value.cells), value.highlight)
    if isinstance(value, tuple):
        return [_value(item) for item in value]
    return value


def _node(value: common.Node) -> render.Html:
    primitive = render.table if value.kind == "table" else getattr(render, value.kind)
    args = tuple(_value(item) for item in value.args)
    kwargs = {key: _value(item) for key, item in value.kwargs.items}
    return primitive(*args, **kwargs)


def render_model(model: common.LegacyViewModel) -> tuple[str, str, render.Html, render.Html]:
    total = _node(model.total) if isinstance(model.total, common.Node) else model.total
    return model.title, model.period, total, _node(model.body)
