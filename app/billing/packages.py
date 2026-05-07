from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VideoPackage:
    code: str
    videos_count: int
    title: str
    price_rub: int
    discount: str | None
    receipt_name: str

    @property
    def amount_cents(self) -> int:
        return self.price_rub * 100

    @property
    def robokassa_out_sum(self) -> str:
        return f"{self.price_rub:.2f}"


VIDEO_PACKAGES: tuple[VideoPackage, ...] = (
    VideoPackage(
        code="videos_10",
        videos_count=10,
        title="10 видео",
        price_rub=99,
        discount=None,
        receipt_name="Пакет 10 видео Fast Promo Videos Bot",
    ),
    VideoPackage(
        code="videos_25",
        videos_count=25,
        title="25 видео",
        price_rub=229,
        discount="скидка 7%",
        receipt_name="Пакет 25 видео Fast Promo Videos Bot",
    ),
    VideoPackage(
        code="videos_50",
        videos_count=50,
        title="50 видео",
        price_rub=399,
        discount="скидка 19%",
        receipt_name="Пакет 50 видео Fast Promo Videos Bot",
    ),
    VideoPackage(
        code="videos_100",
        videos_count=100,
        title="100 видео",
        price_rub=699,
        discount="скидка 29%",
        receipt_name="Пакет 100 видео Fast Promo Videos Bot",
    ),
)


def package_by_videos_count(videos_count: int) -> VideoPackage | None:
    return next((package for package in VIDEO_PACKAGES if package.videos_count == videos_count), None)


def package_by_code(code: str) -> VideoPackage | None:
    return next((package for package in VIDEO_PACKAGES if package.code == code), None)
