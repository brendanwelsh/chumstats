"""Top bar: on phones the utility links (Get the client, How it works) fold
into the #nav-more menu instead of crowding the brand row."""
from chumstats import server


def test_nav_has_phone_menu_holding_util_links():
    html = server._nav("players")
    assert 'id="nav-more"' in html and 'aria-controls="nav-utils"' in html
    utils = html.split('id="nav-utils">', 1)[1].split("</div>", 1)[0]
    assert "/download" in utils and "/about" in utils
    assert "more-open" in html  # toggle script ships with the nav


def test_friend_mode_nav_still_renders_menu():
    html = server._nav("live", friend_mode=True)
    assert "/overlay" in html.split('id="nav-utils">', 1)[1].split("</div>", 1)[0]
