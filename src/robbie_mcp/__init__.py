"""MCP sidecars: read-only context the review model may pull while judging.

One module per provider. Each runs as its own compose service so its token never
enters the container running the model.
"""
