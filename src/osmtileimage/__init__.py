import datetime
import re
from enum import Enum
from typing import Annotated, Optional

import matplotlib as mpl
import matplotlib.colors as colors
import numpy as np
import typer
from PIL import Image, ImageDraw, ImageFont
from pyathena import connect
from pyathena.pandas.cursor import PandasCursor


class Layer(str, Enum):
    raster = "raster"
    vector = "vector"


app = typer.Typer()

DEFAULT_REGION = "eu-north-1"
DEFAULT_WORK_GROUP = "primary"

SAFE_DATE = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
    hours=1, minutes=11
)

CURRENT_HOUR_DATE = datetime.datetime(
    SAFE_DATE.year,
    SAFE_DATE.month,
    SAFE_DATE.day,
    SAFE_DATE.hour,
    tzinfo=datetime.UTC,
)


def date_condition() -> str:
    """
    Generate a SQL WHERE condition for a specific date

    :param date: Date to generate the condition for
    :return: SQL condition for the date
    """
    return "year=%(year)i AND month=%(month)i AND day=%(day)i AND hour=%(hour)i"


def country_callback(value: list[str]) -> list[str]:
    country_pattern = re.compile(r"^[a-z]{2}$", re.IGNORECASE)
    for code in value:
        if not country_pattern.match(code):
            raise typer.BadParameter("Country code must be two letters")
    return [code.upper() for code in value]


def conditions(require_countries: list[str], useragent: Optional[str]) -> str:
    ret = [
        "z >= %(zoom)i",
        "year=%(year)i",
        "month=%(month)i",
        "day=%(day)i",
        "hour=%(hour)i",
    ]

    if require_countries:
        # validated by country_callback above
        countries = ", ".join(f"'{code}'" for code in require_countries)
        ret += [f"country IN ({countries})"]

    if useragent:
        ret += ["useragent LIKE %(useragent)s"]

    return " AND ".join(ret)


# These functions are only ever called once so we don't
# need to worry about mutable defaults
# ruff: disable[B006]


@app.command()
def image(
    layer: Layer = Layer.raster,
    start: datetime.datetime = CURRENT_HOUR_DATE,
    end: datetime.datetime = CURRENT_HOUR_DATE,
    max: Optional[int] = None,
    prefix: str = "tiles-",
    zoom: Annotated[int, typer.Option(min=0, max=12)] = 12,
    require_country: Annotated[
        list[str],
        typer.Option(
            help=(
                "Look at only requests from this country. Repeat for multiple countries"
            ),
            callback=country_callback,
        ),
    ] = [],
    useragent: Optional[str] = None,
    linear_width: int = 10,
    timestamp: Annotated[
        bool, typer.Option(help="Overlay the image's hour in UTC.")
    ] = False,
) -> None:

    match layer:
        case layer.raster:
            if useragent is None:
                tablename = "fastly_raster_location_logs_v4"
                requests_agg = "SUM(requests)"
            else:
                tablename = "fastly_raster_success_logs_v4"
                requests_agg = "COUNT(*)"
        case layer.vector:
            if useragent is None:
                tablename = "fastly_vector_location_logs_v1"
                requests_agg = "SUM(requests)"
            else:
                tablename = "fastly_vector_success_logs_v2"
                requests_agg = "COUNT(*)"

    with (
        connect(
            region_name=DEFAULT_REGION,
            cursor_class=PandasCursor,
            work_group=DEFAULT_WORK_GROUP,
        ) as conn,
        conn.cursor() as curs,
    ):
        cm = mpl.colormaps["cubehelix"].resampled(2048)
        date = start
        while date <= end:
            filename = prefix + date.strftime("%Y-%m-%d-%H") + ".png"
            print(f"Generating {filename}")
            tiles_cte = (
                "WITH tiles AS (\n"
                "SELECT\n"
                f"{requests_agg} AS requests,\n"
                "z, x, y\n"
                f"FROM logs.{tablename}\n"
                f"WHERE {conditions(require_country, useragent)}\n"
                "GROUP BY z, x, y\n"
                ")\n"
            )
            query = (
                tiles_cte + "SELECT\n"
                "bitwise_right_shift(x, z-%(zoom)i) AS x,\n"
                "bitwise_right_shift(y, z-%(zoom)i) AS y,\n"
                "SUM(requests)\n"
                "FROM tiles\n"
                "GROUP BY 1, 2"
            )
            params = {
                "year": date.year,
                "month": date.month,
                "day": date.day,
                "hour": date.hour,
                "zoom": zoom,
                "useragent": useragent,
            }

            curs.execute(query, params)
            requests = np.zeros((1 << zoom, 1 << zoom), dtype=np.uint32)
            for tile in curs:
                requests[tile[1], tile[0]] = tile[2]
            max_requests = np.percentile(requests, [99.99, 99.999, 100.0])

            print(
                f"99.99th percentile: {max_requests[0]}, "
                f"99.999th percentile: {max_requests[1]}, "
                f"100th percentile: {max_requests[2]}"
            )
            if max is None:
                max = int(max_requests[1])
            # https://stackoverflow.com/a/78849568/2607762
            pixels = cm(
                colors.AsinhNorm(linear_width=linear_width, vmin=0, vmax=max)(requests)
            )
            im = Image.fromarray((pixels * 255).astype(np.uint8))
            if timestamp:
                draw = ImageDraw.Draw(im)
                font = ImageFont.load_default(size=32)
                label = date.strftime("%Y-%m-%d %H:00 UTC")
                text_bounds = draw.textbbox((8, 8), label, font=font)
                draw.rectangle(
                    (0, 0, text_bounds[2] + 8, text_bounds[3] + 8),
                    fill=(0, 0, 0, 255),
                )
                draw.text((8, 8), label, font=font, fill=(255, 255, 255, 255))
            im.save(filename)

            date += datetime.timedelta(hours=1)


# ruff: enable[B006]
