"""v2 dashboard: a client-rendered TypeScript/Preact redesign of the cost-analytics UI.

The presentation is a browser app (source in the repo-root ``frontend/`` directory, built
bundle committed to ``dashboard2/assets/``). It reuses the tested data layer
(:mod:`metsuke.viewmodel.overview`) and the hardened dashboard server security unchanged;
only the presentation is zero-based. :mod:`metsuke.dashboard2.web` is the whole Python
surface: it serves the data-free HTML shell, the committed static assets, and the JSON the
client fetches.
"""

from . import web

__all__ = ["web"]
