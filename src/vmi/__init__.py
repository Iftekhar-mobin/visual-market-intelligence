"""Visual Market Intelligence — chart perception as a standalone service.

The package is layered, and the layers only point inwards:

    interfaces/     HTTP API and CLI — how the outside world asks
    application/    agents and the pipeline that sequences them
    domain/         the vocabulary: charts, observations, opportunities, ports
    infrastructure/ the replaceable parts: data feeds, chart drawing, vision models

Nothing in `domain` imports anything else in the package, which is what lets the
vision model, the price feed and the chart renderer all be swapped without the
agents noticing.
"""

__version__ = "0.1.0"
