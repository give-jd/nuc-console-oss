"""The console's lines of one overview card or block, for the tests that read a card as text.

A card is built by its builder in src/cards.py and drawn by ansi.card_lines; these helpers do the two steps for the tests that look at the
words and the widths of one section (the one place that knows each builder and the part of the Ctx it reads). Nothing here is used by the
program itself.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import ansi  # noqa: E402
import cards  # noqa: E402
import nuc_config  # noqa: E402
import screens  # noqa: E402
import ui  # noqa: E402


def card_lines(builder, ctx, w, k=0, caps=None):
    """The lines of the card `builder` makes of `ctx` at detail level k, w columns wide (title included)."""
    return ansi.card_lines(builder(ctx, k, caps or cards.Caps(w)), w)[0]


def _ctx(**kw):
    return cards.Ctx(cfg=nuc_config.current(), **kw)


def ov_sistema(s, w, k, cont=None):
    return card_lines(cards.system_card, _ctx(s=s, cont=cont), w, k)


def ov_container(cont, w, k):
    return card_lines(cards.containers_card, _ctx(cont=cont), w, k)


def ov_database(net, cont, w, k):
    return card_lines(cards.databases_card, _ctx(net=net, cont=cont), w, k)


def ov_esposizione(net, cont, w, k, new=None):
    return card_lines(cards.exposure_card, _ctx(net=net, cont=cont, new=new), w, k)


def ov_firewall(net, w, k):
    return card_lines(cards.firewall_card, _ctx(net=net), w, k)


def ov_boot(b, w, k, now=None):
    return card_lines(cards.boot_card, _ctx(boot=b, now=now), w, k)


def ov_traffico(s, w, k):
    return card_lines(cards.network_traffic_card, _ctx(s=s), w, k)


def ov_sessioni(s, w, k):
    return card_lines(cards.sessions_card, _ctx(s=s), w, k)


def ov_tailscale(net, w, k):
    return card_lines(cards.tailscale_card, _ctx(net=net), w, k)


def ov_webapp(net, cont, w, k):
    return card_lines(cards.webapps_card, _ctx(net=net, cont=cont), w, k)


def ov_docker(boot, w, k):
    return card_lines(cards.docker_disk_card, _ctx(boot=boot), w, k)


def ov_dischi(s, w, k):
    return card_lines(cards.disks_card, _ctx(s=s), w, k)


def ov_attention(pb, w, k):
    return card_lines(cards.attention_card, _ctx(problems=pb), w, k)


def stack_lines(cont, w, cap):
    """The stacks of the containers card: a header with counts and RAM, then the services with a status dot."""
    return [x for part in cards.stack_parts(cont, cap, cards.Caps(w)) for x in ansi.render(part, w)[0]]


def fw_status_lines(net):
    """The two most important status lines of the firewall: ufw and DOCKER-USER (macOS/Windows: the OS firewall)."""
    return [x for n in cards.fw_status(net) for x in ansi.render(n, 80)[0]]


def native_fw_details(net, w, max_rules=None):
    """macOS/Windows FIREWALL body: the configuration in a few lines, then which rule or setting opens each listening port."""
    return [x for n in cards.fw_native_details(net, cards.Caps(w), max_rules) for x in ansi.render(n, w)[0]]


def ai_hw_lines(hw, w, k=0):
    """The HARDWARE block of the AI screen."""
    return ansi.render(ui.Group(screens.ai_hw_nodes(hw, w, k)), w)[0]


def map_title(G, w, only=False):
    """The Map's heading line (title, figures, legend), w columns wide."""
    return ansi.render(screens.map_title(G, only), w)[0][0]


def map_row(G, row):
    """One tree row of the Map as an ANSI string, not cut."""
    b = screens.map_branch(G, row)
    return (ansi.style(b.prefix, "muted") if b.prefix else "") + ansi.inline(b.mark) + " " + ansi.inline(b.body)
