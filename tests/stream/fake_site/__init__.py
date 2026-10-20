"""Local fake feed site used by the streaming spine tests and dev host."""

from tests.stream.fake_site.server import (  # noqa: F401
    FakeSite,
    make_comments,
    make_feed_page,
    make_post,
    start_fake_site,
)
