from metsuke.viewmodel.cache import query

from .model_renderer import render_model


def build(conn, window):
    return render_model(query(conn, window))
