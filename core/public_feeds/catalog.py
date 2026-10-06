from dataclasses import dataclass
from typing import Literal

Market = Literal["ZA", "NG", "KE"]
FeedKind = Literal["news", "chart", "playlist"]


@dataclass(frozen=True, slots=True)
class FeedSpec:
    feed_id: str
    name: str
    market: Market
    url: str
    kind: FeedKind
    supported: bool
    reason: str | None = None


FEEDS: tuple[FeedSpec, ...] = (
    FeedSpec("za_enca", "eNCA", "ZA", "https://www.enca.com/", "news", True),
    FeedSpec("za_groundup", "GroundUp", "ZA", "https://groundup.news/", "news", True),
    FeedSpec("za_timeslive", "TimesLIVE", "ZA", "https://www.timeslive.co.za/", "news", True),
    # IOL (iol.co.za) left out: it refuses our reader with HTTP 403, as it has refused bot user agents since Apr 2026.
    FeedSpec("za_the_citizen", "The Citizen", "ZA", "https://www.citizen.co.za/", "news", True),
    FeedSpec("za_mail_and_guardian", "Mail & Guardian", "ZA", "https://mg.co.za/", "news", True),
    FeedSpec("za_business_day", "Business Day", "ZA", "https://www.businessday.co.za/", "news", True),
    FeedSpec("za_bizcommunity", "Bizcommunity South Africa", "ZA", "https://www.bizcommunity.com/", "news", True),
    FeedSpec("za_cape_town_etc", "Cape Town Etc", "ZA", "https://www.capetownetc.com/", "news", True),
    FeedSpec("za_sabc_news", "SABC News", "ZA", "https://www.sabcnews.com/sabcnews/", "news", True),
    FeedSpec("za_the_south_african", "The South African", "ZA", "https://www.thesouthafrican.com/", "news", True),
    FeedSpec("za_businesstech", "BusinessTech", "ZA", "https://businesstech.co.za/news/", "news", True),
    FeedSpec("za_sowetan_news", "Sowetan News", "ZA", "https://www.sowetan.co.za/news/", "news", True),
    FeedSpec("za_current_affairs_za", "Current Affairs ZA", "ZA", "https://currentaffairsza.com/", "news", True),
    FeedSpec("za_the_herald", "The Herald", "ZA", "https://theherald.co.za/", "news", True),
    FeedSpec(
        "ng_turntable_top_100",
        "TurnTable Official Nigeria Top 100",
        "NG",
        "https://www.turntablecharts.com/charts/1",
        "chart",
        True,
    ),
    FeedSpec("ng_channels_television", "Channels Television", "NG", "https://www.channelstv.com/", "news", True),
    FeedSpec("ng_arise_news", "Arise News", "NG", "https://www.arise.tv/", "news", True),
    FeedSpec("ng_punch", "Punch", "NG", "https://punchng.com/", "news", True),
    FeedSpec("ng_premium_times", "Premium Times", "NG", "https://www.premiumtimesng.com/", "news", True),
    FeedSpec("ng_guardian", "The Guardian Nigeria", "NG", "https://guardian.ng/", "news", True),
    FeedSpec("ng_thecable", "TheCable", "NG", "https://www.thecable.ng/", "news", True),
    FeedSpec("ng_bellanaija", "BellaNaija", "NG", "https://www.bellanaija.com/", "news", True),
    FeedSpec("ng_notjustok", "Notjustok", "NG", "https://notjustok.com/", "news", True),
    FeedSpec("ng_tooxclusive", "tooXclusive", "NG", "https://tooxclusive.com/", "news", True),
    FeedSpec("ng_vanguard", "Vanguard", "NG", "https://www.vanguardngr.com/", "news", True),
    FeedSpec("ng_legit", "Legit Nigeria", "NG", "https://www.legit.ng/", "news", True),
    FeedSpec("ng_pulse", "Pulse Nigeria", "NG", "https://www.pulse.ng/", "news", True),
    FeedSpec("ng_nairametrics", "Nairametrics", "NG", "https://nairametrics.com/", "news", True),
    FeedSpec("ng_pm_news", "PM News", "NG", "https://pmnewsnigeria.com/", "news", True),
    FeedSpec(
        "ke_mdundo_top_songs",
        "Mdundo Top Songs: Kenya",
        "KE",
        "https://play.mdundo.com/top-charts/ke",
        "chart",
        True,
    ),
    FeedSpec(
        "ke_rotation_chart",
        "rotation. Kenya chart",
        "KE",
        "https://www.rotation.africa/charts/ke",
        "chart",
        False,
        "Upstream extraction and independence from Kworb are unconfirmed.",
    ),
    FeedSpec(
        "ke_kbc_english_playlist",
        "KBC English Service playlist via Radio.co",
        "KE",
        "https://radio.co.ke/listen/kbc-english-service/",
        "playlist",
        True,
    ),
    FeedSpec(
        "ke_capital_fm_playlist",
        "Capital FM playlist via Radio.co",
        "KE",
        "https://radio.co.ke/listen/capital-fm/",
        "playlist",
        True,
    ),
    FeedSpec("ke_citizen_digital", "Citizen Digital", "KE", "https://citizen.digital/", "news", True),
    FeedSpec("ke_the_standard", "The Standard", "KE", "https://www.standardmedia.co.ke/", "news", True),
    FeedSpec("ke_k24_digital", "K24 Digital", "KE", "https://k24.digital/", "news", True),
    FeedSpec("ke_capital_fm_kenya", "Capital FM Kenya", "KE", "https://capitalfm.africa/", "news", True),
    FeedSpec("ke_ghafla", "Ghafla Kenya", "KE", "https://www.ghafla.co.ke/", "news", True),
    FeedSpec("ke_mpasho", "Mpasho", "KE", "https://www.mpasho.co.ke/", "news", True),
    FeedSpec("ke_kbc_digital", "KBC Digital", "KE", "https://www.kbc.co.ke/", "news", True),
    FeedSpec("ke_tuko", "Tuko", "KE", "https://www.tuko.co.ke/", "news", True),
    FeedSpec("ke_kenyans_co_ke", "Kenyans.co.ke", "KE", "https://www.kenyans.co.ke/", "news", True),
    FeedSpec("ke_the_star_kenya", "The Star Kenya", "KE", "https://www.the-star.co.ke/", "news", True),
    FeedSpec("ke_techweez", "Techweez", "KE", "https://techweez.com/", "news", True),
)


def confirmed_feeds(market: Market | None = None) -> tuple[FeedSpec, ...]:
    return tuple(
        feed
        for feed in FEEDS
        if feed.supported and (market is None or feed.market == market)
    )
