import argparse
import csv
import json
import platform
import subprocess
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
from plyer import notification
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
PRICE_LOG_FILE = BASE_DIR / "price_log.csv"
SIGNAL_LOG_FILE = BASE_DIR / "signal_log.csv"
LATEST_SIGNAL_FILE = BASE_DIR / "latest_signal.json"
YOUPIN_API_BASE = "https://api.youpin898.com"
YOUPIN_WEB_BASE = "https://www.youpin898.com"
YOUPIN_DETAIL_API = f"{YOUPIN_API_BASE}/api/commodity/Commodity/Detail"
YOUPIN_LIST_API = f"{YOUPIN_API_BASE}/api/youpin/pc/inventory/list"
YOUPIN_TEMPLATE_DETAIL_API = (
    f"{YOUPIN_API_BASE}/api/homepage/pc/goods/market/queryTemplateDetail"
)
YOUPIN_HOMEPAGE_MARKET_API = (
    f"{YOUPIN_API_BASE}/api/homepage/pc/goods/market/queryOnSaleCommodityList"
)


class YoupinAuthError(RuntimeError):
    """悠悠有品登录态失效，需要用户重新登录。"""


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def print_status(message: str) -> None:
    print(f"[{now_text()}] {message}")


def load_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"找不到配置文件: {CONFIG_FILE}")

    raw_text = CONFIG_FILE.read_text(encoding="utf-8").strip()
    if not raw_text:
        raise ValueError("config.json 还是空的，请先填写配置后再运行。")

    config = json.loads(raw_text)
    validate_config(config)
    return config


def validate_config(config: dict[str, Any]) -> None:
    if config.get("platform", "youpin") != "youpin":
        raise ValueError("当前脚本是悠悠有品版，请把 platform 设置成 youpin。")

    items = config.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("config.json 里的 items 必须是非空列表。")

    for index, item in enumerate(items, start=1):
        has_detail_mode = item.get("commodity_id") is not None or bool(item.get("commodity_no"))
        has_template_mode = item.get("template_id") is not None
        if not has_detail_mode and not has_template_mode:
            raise ValueError(
                f"第 {index} 个商品缺少 commodity_id、commodity_no 或 template_id。"
            )

        auth_config = config.get("auth", {})
        has_legacy_cookie = bool(auth_config.get("cookie_header"))
        has_market_token = bool(
            auth_config.get("authorization")
            or auth_config.get("extra_headers", {}).get("authorization")
        )
        if has_template_mode and not (has_legacy_cookie or has_market_token):
            raise ValueError(
                "template_id 列表监控模式至少需要 auth.cookie_header，或者 auth.authorization。"
            )

        rule_fields = ["buy_below", "sell_above", "sell_below", "drop_below"]
        has_direct_rule = any(item.get(field) is not None for field in rule_fields)
        has_max_drop_rule = (
            item.get("buy_price") is not None and item.get("max_drop") is not None
        )
        if not has_direct_rule and not has_max_drop_rule:
            raise ValueError(
                (
                    f"第 {index} 个商品没有可用规则。"
                    "请至少填写 buy_below、sell_above、sell_below、drop_below 中的一个，"
                    "或者填写 buy_price + max_drop。"
                )
            )

        market_api_mode = str(item.get("market_api", config.get("market_api", "auto")))
        if has_template_mode and market_api_mode in ("homepage_market", "auto"):
            validate_homepage_market_auth(config)


def is_placeholder_value(value: Any) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    placeholder_markers = ["请替换", "这里换成", "queryOnSaleCommodityList 这条请求里的"]
    return any(marker in text for marker in placeholder_markers)


def validate_header_value_ascii(field_name: str, value: Any) -> None:
    text = str(value).strip()
    try:
        text.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"auth.{field_name} 里还是中文示例文本，必须改成浏览器请求头里的真实值。"
        ) from exc


def validate_homepage_market_auth(config: dict[str, Any]) -> None:
    auth_config = config.get("auth", {})
    required_fields = ["authorization", "deviceid", "deviceuk", "uk"]
    for field_name in required_fields:
        field_value = auth_config.get(field_name)
        if is_placeholder_value(field_value):
            raise ValueError(
                f"auth.{field_name} 还没有替换成真实请求头。请去抓 "
                "/api/homepage/pc/goods/market/queryOnSaleCommodityList 这条请求后再填。"
            )
        validate_header_value_ascii(field_name, field_value)


def create_session(config: dict[str, Any]) -> requests.Session:
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry)

    user_agent = config.get("auth", {}).get(
        "user_agent",
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
    )

    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Origin": "https://www.youpin898.com",
            "Referer": "https://www.youpin898.com/",
            "Accept": "application/json, text/plain, */*",
        }
    )

    cookie_header = config.get("auth", {}).get("cookie_header")
    if cookie_header and not is_placeholder_value(cookie_header):
        session.headers["Cookie"] = cookie_header

    auth_config = config.get("auth", {})
    header_mappings = [
        ("authorization", "authorization"),
        ("deviceid", "deviceid"),
        ("deviceid", "deviceId"),
        ("deviceuk", "deviceuk"),
        ("deviceuk", "deviceUk"),
        ("uk", "uk"),
        ("app_version", "app-version"),
        ("appversion", "AppVersion"),
        ("apptype", "apptype"),
        ("apptype", "appType"),
        ("appversion", "appversion"),
        ("platform", "platform"),
        ("secret_v", "secret-v"),
    ]
    for config_key, header_key in header_mappings:
        header_value = auth_config.get(config_key)
        if header_value and not is_placeholder_value(header_value):
            session.headers[header_key] = str(header_value)

    extra_headers = auth_config.get("extra_headers", {})
    if isinstance(extra_headers, dict):
        session.headers.update({str(key): str(value) for key, value in extra_headers.items()})

    return session


def get_youpin_detail(
    session: requests.Session,
    item: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if item.get("commodity_id") is not None:
        params["id"] = item["commodity_id"]
    elif item.get("commodity_no"):
        params["commodityNo"] = item["commodity_no"]

    response = session.get(YOUPIN_DETAIL_API, params=params, timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    if payload.get("Code") != 0:
        raise ValueError(payload.get("Msg") or "悠悠有品详情接口返回失败。")
    return payload["Data"]


def has_homepage_market_auth(config: dict[str, Any]) -> bool:
    auth_config = config.get("auth", {})
    extra_headers = auth_config.get("extra_headers", {})
    return bool(
        auth_config.get("authorization")
        or (isinstance(extra_headers, dict) and extra_headers.get("authorization"))
    )


def get_market_api_mode(config: dict[str, Any], item: dict[str, Any]) -> str:
    return str(item.get("market_api", config.get("market_api", "auto")))


def get_youpin_template_list_legacy(
    session: requests.Session,
    config: dict[str, Any],
    item: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    payload = {
        "gameId": item.get("game_id", config.get("game_id", 730)),
        "templateId": item["template_id"],
        "listType": item.get("list_type", 10),
        "pageIndex": item.get("page_index", 1),
        "pageSize": item.get("page_size", 80),
    }

    response = session.post(YOUPIN_LIST_API, json=payload, timeout=timeout_seconds)
    response.raise_for_status()
    body = response.json()
    code = body.get("code")
    if code == 84101:
        raise YoupinAuthError(body.get("msg") or "登录信息已失效，请重新登录悠悠有品。")
    if code not in (0, None):
        raise ValueError(body.get("msg") or "悠悠有品列表接口返回失败。")
    return body


def build_homepage_market_payload(
    config: dict[str, Any],
    item: dict[str, Any],
) -> dict[str, Any]:
    # 这里对齐你现在网页里 queryOnSaleCommodityList 的请求体。
    payload: dict[str, Any] = {
        "gameId": get_game_id(config, item),
        "listType": item.get("list_type", 10),
        "templateId": item["template_id"],
        "pageIndex": item.get("page_index", 1),
        "pageSize": item.get("page_size", 80),
        "sortTypeKey": item.get("sort_type_key", "Id"),
        "listSortType": item.get("list_sort_type", 1),
        "sortType": item.get("sort_type", 0),
    }

    optional_field_mappings = {
        "paint_seed": "paintSeed",
        "min_abrade": "minAbrade",
        "max_abrade": "maxAbrade",
        "have_name_tag": "haveNameTag",
        "have_sticker": "haveSticker",
        "commodity_stickers_tag": "commodityStickersTag",
        "have_buzhang_type": "haveBuZhangType",
        "have_buzhang": "haveBuZhang",
        "min_fade_val": "minFadeVal",
        "max_fade_val": "maxFadeVal",
        "min_price": "minPrice",
        "max_price": "maxPrice",
        "keywords": "keyWords",
        "pendent_template": "pendentTemplate",
        "pendent_type": "pendentType",
    }
    for item_key, payload_key in optional_field_mappings.items():
        if item.get(item_key) is not None:
            payload[payload_key] = item[item_key]

    return payload


def get_youpin_template_list_homepage(
    session: requests.Session,
    config: dict[str, Any],
    item: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    payload = build_homepage_market_payload(config, item)
    response = session.post(YOUPIN_HOMEPAGE_MARKET_API, json=payload, timeout=timeout_seconds)
    response.raise_for_status()
    body = response.json()
    code = body.get("Code")
    if code in (84103, 84101, 2002):
        raise YoupinAuthError(body.get("Msg") or "登录信息已失效，请重新登录悠悠有品。")
    if code != 0:
        raise ValueError(body.get("Msg") or "悠悠有品新版商品列表接口返回失败。")
    return body


def get_youpin_template_detail_homepage(
    session: requests.Session,
    config: dict[str, Any],
    item: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    payload = {
        "templateId": item["template_id"],
        "gameId": get_game_id(config, item),
        "listType": item.get("list_type", 10),
    }
    response = session.post(YOUPIN_TEMPLATE_DETAIL_API, json=payload, timeout=timeout_seconds)
    response.raise_for_status()
    body = response.json()
    code = body.get("Code")
    if code in (84103, 84101, 2002):
        raise YoupinAuthError(body.get("Msg") or "登录信息已失效，请重新登录悠悠有品。")
    if code != 0:
        raise ValueError(body.get("Msg") or "悠悠有品模板详情接口返回失败。")
    return body


def get_youpin_template_list(
    session: requests.Session,
    config: dict[str, Any],
    item: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    market_api_mode = get_market_api_mode(config, item)

    if market_api_mode == "homepage_market":
        return get_youpin_template_list_homepage(session, config, item, timeout_seconds)
    if market_api_mode == "legacy_inventory":
        return get_youpin_template_list_legacy(session, config, item, timeout_seconds)

    # auto 模式下优先走你当前网页真实使用的新接口；如果没有授权头，再回退旧接口。
    if has_homepage_market_auth(config):
        try:
            return get_youpin_template_list_homepage(session, config, item, timeout_seconds)
        except YoupinAuthError:
            raise
        except Exception:
            if config.get("auth", {}).get("cookie_header"):
                return get_youpin_template_list_legacy(session, config, item, timeout_seconds)
            raise

    return get_youpin_template_list_legacy(session, config, item, timeout_seconds)


def get_display_name(item: dict[str, Any], fallback_name: str) -> str:
    return item.get("display_name") or fallback_name


def build_youpin_detail_api_url(commodity_id: Any = None, commodity_no: Any = None) -> str:
    params: dict[str, str] = {}
    if commodity_id is not None:
        params["id"] = str(commodity_id)
    elif commodity_no:
        params["commodityNo"] = str(commodity_no)
    return f"{YOUPIN_DETAIL_API}?{urlencode(params)}"


def build_youpin_goods_list_url(
    template_id: Any,
    game_id: Any = 730,
    list_type: Any = 10,
) -> str:
    # 悠悠有品商品页和价格走势页都是同一个路由，只是 listType 不同。
    params = {
        "listType": str(list_type),
        "templateId": str(template_id),
        "gameId": str(game_id),
    }
    return f"{YOUPIN_WEB_BASE}/market/goods-list?{urlencode(params)}"


def get_game_id(config: dict[str, Any], item: dict[str, Any]) -> Any:
    return item.get("game_id", config.get("game_id", 730))


def resolve_market_url(
    config: dict[str, Any],
    item: dict[str, Any],
    fallback_url: str | None = None,
) -> str:
    if item.get("detail_url"):
        return str(item["detail_url"])
    if item.get("template_id") is not None:
        return build_youpin_goods_list_url(
            template_id=item["template_id"],
            game_id=get_game_id(config, item),
            list_type=item.get("list_type", 10),
        )
    return fallback_url or ""


def resolve_price_trend_url(config: dict[str, Any], item: dict[str, Any]) -> str:
    if item.get("price_trend_url"):
        return str(item["price_trend_url"])
    if item.get("template_id") is not None:
        return build_youpin_goods_list_url(
            template_id=item["template_id"],
            game_id=get_game_id(config, item),
            list_type="price",
        )
    return ""


def resolve_login_url(config: dict[str, Any], item: dict[str, Any]) -> str:
    login_url = item.get("login_url") or config.get("auth", {}).get("login_url")
    if login_url:
        return str(login_url)
    return YOUPIN_WEB_BASE


def get_item_label(item: dict[str, Any]) -> str:
    if item.get("display_name"):
        return str(item["display_name"])
    if item.get("template_id") is not None:
        return f"templateId={item['template_id']}"
    if item.get("commodity_id") is not None:
        return f"commodity_id={item['commodity_id']}"
    if item.get("commodity_no"):
        return f"commodity_no={item['commodity_no']}"
    return "未命名商品"


def get_numeric_value(source: dict[str, Any], keys: list[str]) -> float | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return None


def collect_price_records(node: Any, bucket: list[dict[str, Any]]) -> None:
    if isinstance(node, dict):
        price_value = get_numeric_value(
            node,
            ["Price", "price", "SalePrice", "salePrice", "SellPrice", "sellPrice", "purchasePrice"],
        )
        if price_value is not None:
            bucket.append(node)
        for value in node.values():
            collect_price_records(value, bucket)
    elif isinstance(node, list):
        for value in node:
            collect_price_records(value, bucket)


def pick_lowest_price_record(payload: dict[str, Any]) -> dict[str, Any]:
    # 新版首页商品列表接口会把商品记录直接放在 Data 数组里，优先按这个结构取。
    data_records = payload.get("Data")
    if isinstance(data_records, list):
        direct_records = [
            record
            for record in data_records
            if isinstance(record, dict)
            and get_numeric_value(
                record,
                ["Price", "price", "SalePrice", "salePrice", "SellPrice", "sellPrice"],
            )
            is not None
        ]
        if direct_records:
            direct_records.sort(
                key=lambda record: get_numeric_value(
                    record,
                    ["Price", "price", "SalePrice", "salePrice", "SellPrice", "sellPrice"],
                )
                or float("inf")
            )
            return direct_records[0]

    records: list[dict[str, Any]] = []
    collect_price_records(payload, records)
    if not records:
        raise ValueError("列表接口返回了数据，但没有找到可解析的价格记录。")

    records.sort(
        key=lambda record: get_numeric_value(
            record,
            ["Price", "price", "SalePrice", "salePrice", "SellPrice", "sellPrice"],
        )
        or float("inf")
    )
    return records[0]


def build_snapshot_from_detail(item: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    display_name = get_display_name(item, detail.get("CommodityName") or "未命名商品")
    current_price = get_numeric_value(detail, ["Price", "price"])
    reference_price = get_numeric_value(detail, ["MarkPrice", "markPrice"])
    market_url = (
        item.get("detail_url")
        or detail.get("H5Url")
        or detail.get("PcUrl")
        or build_youpin_detail_api_url(detail.get("Id"), detail.get("CommodityNo"))
    )

    return {
        "item_name": display_name,
        "current_price": current_price,
        "reference_price": reference_price,
        "commodity_id": detail.get("Id", item.get("commodity_id")),
        "commodity_no": detail.get("CommodityNo", item.get("commodity_no")),
        "detail_url": market_url,
        "market_url": market_url,
        "price_trend_url": resolve_price_trend_url({}, item),
        "state_text": "可售" if detail.get("IsCanSold") else "不可售",
    }


def build_snapshot_from_template(
    config: dict[str, Any],
    item: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    lowest_record = pick_lowest_price_record(payload)
    display_name = get_display_name(
        item,
        lowest_record.get("CommodityName")
        or lowest_record.get("commodityName")
        or lowest_record.get("CommodityHashName")
        or lowest_record.get("commodityHashName")
        or f"模板 {item['template_id']}",
    )
    current_price = get_numeric_value(
        lowest_record,
        ["Price", "price", "SalePrice", "salePrice", "SellPrice", "sellPrice"],
    )
    reference_price = get_numeric_value(lowest_record, ["MarkPrice", "markPrice"])
    commodity_id = lowest_record.get("Id", lowest_record.get("id"))
    commodity_no = lowest_record.get("CommodityNo", lowest_record.get("commodityNo"))
    market_url = resolve_market_url(
        config=config,
        item=item,
        fallback_url=lowest_record.get("H5Url")
        or lowest_record.get("PcUrl")
        or build_youpin_detail_api_url(commodity_id, commodity_no),
    )
    price_trend_url = resolve_price_trend_url(config, item)

    return {
        "item_name": display_name,
        "current_price": current_price,
        "reference_price": reference_price,
        "commodity_id": commodity_id,
        "commodity_no": commodity_no,
        "detail_url": market_url,
        "market_url": market_url,
        "price_trend_url": price_trend_url,
        "state_text": "列表最低价",
    }


def pick_template_detail_price_record(
    item: dict[str, Any],
    detail_payload: dict[str, Any],
) -> dict[str, Any] | None:
    filters = detail_payload.get("filters") or []
    template_id_text = str(item["template_id"])
    matched_records: list[dict[str, Any]] = []
    fallback_records: list[dict[str, Any]] = []

    for filter_node in filters:
        if not isinstance(filter_node, dict):
            continue
        if filter_node.get("FilterKey") != "Exterior":
            continue
        for option in filter_node.get("Items") or []:
            if not isinstance(option, dict):
                continue
            price_value = get_numeric_value(option, ["SellPrice", "sellPrice", "Price", "price"])
            if price_value is None:
                continue
            fallback_records.append(option)
            if str(option.get("FixedVal", "")) == template_id_text:
                matched_records.append(option)

    if matched_records:
        matched_records.sort(
            key=lambda record: get_numeric_value(
                record,
                ["SellPrice", "sellPrice", "Price", "price"],
            )
            or float("inf")
        )
        return matched_records[0]

    if fallback_records:
        fallback_records.sort(
            key=lambda record: get_numeric_value(
                record,
                ["SellPrice", "sellPrice", "Price", "price"],
            )
            or float("inf")
        )
        return fallback_records[0]

    return None


def build_snapshot_from_template_detail(
    config: dict[str, Any],
    item: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    data = payload.get("Data") or {}
    template_info = data.get("templateInfo") or {}
    price_record = pick_template_detail_price_record(item, data)
    display_name = get_display_name(
        item,
        template_info.get("commodityName") or f"模板 {item['template_id']}",
    )
    current_price = None
    if price_record is not None:
        current_price = get_numeric_value(price_record, ["SellPrice", "sellPrice", "Price", "price"])
    reference_price = get_numeric_value(
        template_info,
        ["steamPrice", "SteamPrice", "steamUSDPrice"],
    )
    market_url = resolve_market_url(config=config, item=item)
    price_trend_url = resolve_price_trend_url(config, item)

    state_parts: list[str] = []
    sell_number = template_info.get("sellNumber")
    if sell_number is not None:
        state_parts.append(f"在售 {sell_number}")
    matched_name = price_record.get("Name") if isinstance(price_record, dict) else None
    if matched_name:
        state_parts.append(str(matched_name))
    state_text = "，".join(state_parts) if state_parts else "模板详情"

    return {
        "item_name": display_name,
        "current_price": current_price,
        "reference_price": reference_price,
        "commodity_id": template_info.get("id", item.get("template_id")),
        "commodity_no": template_info.get("commodityHashName", ""),
        "detail_url": market_url,
        "market_url": market_url,
        "price_trend_url": price_trend_url,
        "state_text": state_text,
    }


def resolve_item_snapshot(
    session: requests.Session,
    config: dict[str, Any],
    item: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    if item.get("template_id") is not None:
        market_api_mode = get_market_api_mode(config, item)
        if market_api_mode == "homepage_market":
            payload = get_youpin_template_detail_homepage(session, config, item, timeout_seconds)
            return build_snapshot_from_template_detail(config, item, payload)
        payload = get_youpin_template_list(session, config, item, timeout_seconds)
        return build_snapshot_from_template(config, item, payload)

    detail = get_youpin_detail(session, item, timeout_seconds)
    return build_snapshot_from_detail(item, detail)


def send_desktop_notification(title: str, message: str) -> None:
    try:
        notification.notify(title=title, message=message, timeout=10)
    except Exception as exc:
        print_status(f"桌面提醒发送失败: {exc}")


def write_price_log(
    item_name: str,
    commodity_id: Any,
    commodity_no: Any,
    current_price: float | None,
    reference_price: float | None,
    state_text: str,
) -> None:
    file_exists = PRICE_LOG_FILE.exists()
    with PRICE_LOG_FILE.open("a", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        if not file_exists:
            writer.writerow(
                [
                    "time",
                    "item_name",
                    "commodity_id",
                    "commodity_no",
                    "current_price",
                    "reference_price",
                    "state",
                ]
            )
        writer.writerow(
            [
                now_text(),
                item_name,
                commodity_id if commodity_id is not None else "",
                commodity_no or "",
                current_price if current_price is not None else "",
                reference_price if reference_price is not None else "",
                state_text,
            ]
        )


def write_signal_log(signal: dict[str, Any]) -> None:
    file_exists = SIGNAL_LOG_FILE.exists()
    with SIGNAL_LOG_FILE.open("a", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        if not file_exists:
            writer.writerow(
                [
                    "time",
                    "item_name",
                    "signal_type",
                    "action_side",
                    "current_price",
                    "target_price",
                    "message",
                    "detail_url",
                    "price_trend_url",
                ]
            )
        writer.writerow(
            [
                signal["time"],
                signal["item_name"],
                signal["signal_type"],
                signal["action_side"],
                signal["current_price"],
                signal["target_price"],
                signal["message"],
                signal["detail_url"],
                signal.get("price_trend_url", ""),
            ]
        )

    LATEST_SIGNAL_FILE.write_text(
        json.dumps(signal, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def should_notify(
    item_name: str,
    cooldown_minutes: int,
    notification_state: dict[str, float],
) -> bool:
    if cooldown_minutes <= 0:
        return True

    last_sent_ts = notification_state.get(item_name)
    if last_sent_ts is None:
        return True

    return (time.time() - last_sent_ts) >= cooldown_minutes * 60


def mark_notified(item_name: str, notification_state: dict[str, float]) -> None:
    notification_state[item_name] = time.time()


def get_item_cooldown_minutes(config: dict[str, Any], item: dict[str, Any]) -> int:
    return int(
        item.get(
            "notification_cooldown_minutes",
            config.get("notification_cooldown_minutes", 30),
        )
    )


def get_actions_config(config: dict[str, Any]) -> dict[str, bool]:
    actions = config.get("actions", {})
    return {
        "open_market_url": bool(actions.get("open_market_url", True)),
        "open_price_trend_url": bool(actions.get("open_price_trend_url", False)),
        "open_login_url_on_auth_error": bool(actions.get("open_login_url_on_auth_error", True)),
        "copy_target_price": bool(actions.get("copy_target_price", True)),
        "export_signal": bool(actions.get("export_signal", True)),
    }


def copy_text_to_clipboard(text: str) -> bool:
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        root.destroy()
        return True
    except Exception:
        pass

    system_name = platform.system()
    try:
        if system_name == "Windows":
            subprocess.run(["cmd", "/c", "clip"], input=text, text=True, check=True)
            return True
        if system_name == "Darwin":
            subprocess.run(["pbcopy"], input=text, text=True, check=True)
            return True
    except Exception:
        return False

    return False


def open_market_page(url: str) -> bool:
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def open_urls(urls: list[str], action_label: str) -> None:
    opened_urls: set[str] = set()
    for url in urls:
        if not url or url in opened_urls:
            continue
        opened_urls.add(url)
        if open_market_page(url):
            print_status(f"[动作] {action_label}已尝试打开: {url}")
        else:
            print_status(f"[动作] 无法自动打开链接，请手动打开: {url}")


def build_triggered_signals(
    item_name: str,
    item: dict[str, Any],
    current_price: float,
    market_url: str,
    price_trend_url: str,
) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []

    buy_below = item.get("buy_below")
    if buy_below is not None and current_price <= float(buy_below):
        signals.append(
            {
                "signal_type": "buy_below",
                "action_side": "buy",
                "target_price": float(buy_below),
                "message": f"{item_name} 当前价格 {current_price:.2f}，达到低价买入提醒价 {float(buy_below):.2f}",
                "detail_url": market_url,
                "price_trend_url": price_trend_url,
            }
        )

    sell_above = item.get("sell_above")
    if sell_above is not None and current_price >= float(sell_above):
        signals.append(
            {
                "signal_type": "sell_above",
                "action_side": "sell",
                "target_price": float(sell_above),
                "message": f"{item_name} 当前价格 {current_price:.2f}，达到止盈卖出提醒价 {float(sell_above):.2f}",
                "detail_url": market_url,
                "price_trend_url": price_trend_url,
            }
        )

    sell_below = item.get("sell_below")
    if sell_below is None and item.get("drop_below") is not None:
        sell_below = item["drop_below"]
    if sell_below is not None and current_price <= float(sell_below):
        signals.append(
            {
                "signal_type": "sell_below",
                "action_side": "sell",
                "target_price": float(sell_below),
                "message": f"{item_name} 当前价格 {current_price:.2f}，达到止损卖出提醒价 {float(sell_below):.2f}",
                "detail_url": market_url,
                "price_trend_url": price_trend_url,
            }
        )

    buy_price = item.get("buy_price")
    max_drop = item.get("max_drop")
    if buy_price is not None and max_drop is not None:
        stop_price = float(buy_price) - float(max_drop)
        if current_price <= stop_price:
            dropped = float(buy_price) - current_price
            signals.append(
                {
                    "signal_type": "max_drop",
                    "action_side": "sell",
                    "target_price": stop_price,
                    "message": (
                        f"{item_name} 当前价格 {current_price:.2f}，较买入价 {float(buy_price):.2f} "
                        f"已下跌 {dropped:.2f}，达到最大回撤 {float(max_drop):.2f}"
                    ),
                    "detail_url": market_url,
                    "price_trend_url": price_trend_url,
                }
            )

    return signals


def perform_signal_actions(
    config: dict[str, Any],
    item_name: str,
    current_price: float,
    signals: list[dict[str, Any]],
) -> None:
    if not signals:
        return

    actions = get_actions_config(config)
    primary_signal = signals[0]

    urls_to_open: list[str] = []
    if actions["open_market_url"]:
        urls_to_open.append(primary_signal["detail_url"])
    if actions["open_price_trend_url"]:
        urls_to_open.append(primary_signal.get("price_trend_url", ""))
    if urls_to_open:
        open_urls(urls_to_open, "信号触发后")

    if actions["copy_target_price"]:
        target_text = f"{primary_signal['target_price']:.2f}"
        if copy_text_to_clipboard(target_text):
            print_status(f"[动作] 已复制目标价格到剪贴板: {target_text}")
        else:
            print_status("[动作] 复制目标价格失败，请手动复制。")

    if actions["export_signal"]:
        signal_time = now_text()
        for signal in signals:
            write_signal_log(
                {
                    "time": signal_time,
                    "item_name": item_name,
                    "signal_type": signal["signal_type"],
                    "action_side": signal["action_side"],
                    "current_price": round(current_price, 4),
                    "target_price": signal["target_price"],
                    "message": signal["message"],
                    "detail_url": signal["detail_url"],
                    "price_trend_url": signal.get("price_trend_url", ""),
                }
            )
        print_status(f"[动作] 已导出 {len(signals)} 条信号到 {SIGNAL_LOG_FILE.name}")


def handle_auth_error(
    config: dict[str, Any],
    item: dict[str, Any],
    notification_state: dict[str, float],
    exc: YoupinAuthError,
) -> None:
    item_name = get_item_label(item)
    cooldown_minutes = get_item_cooldown_minutes(config, item)
    auth_state_key = f"auth::{item_name}"

    if not should_notify(auth_state_key, cooldown_minutes, notification_state):
        print_status(f"[已抑制重复提醒] {item_name} 登录仍然失效，但还在冷却时间内。")
        return

    print_status(f"[登录失效] {item_name}: {exc}")
    summary = f"{item_name} 登录状态失效，已尝试打开登录页与监控页，请登录后直接买卖。"
    send_desktop_notification("悠悠有品登录失效", summary)

    actions = get_actions_config(config)
    urls_to_open: list[str] = []
    if actions["open_login_url_on_auth_error"]:
        urls_to_open.append(resolve_login_url(config, item))
    if actions["open_market_url"]:
        urls_to_open.append(resolve_market_url(config, item))
    if actions["open_price_trend_url"]:
        urls_to_open.append(resolve_price_trend_url(config, item))
    open_urls(urls_to_open, "登录辅助")
    mark_notified(auth_state_key, notification_state)


def check_items(
    config: dict[str, Any],
    session: requests.Session,
    notification_state: dict[str, float],
) -> None:
    timeout_seconds = int(config.get("request_timeout_seconds", 15))
    request_pause_seconds = float(config.get("request_pause_seconds", 3))

    items = config["items"]
    for index, item in enumerate(items, start=1):
        try:
            snapshot = resolve_item_snapshot(session, config, item, timeout_seconds)
            item_name = snapshot["item_name"]
            current_price = snapshot["current_price"]
            reference_price = snapshot["reference_price"]

            print("-" * 70)
            print_status(f"检查第 {index}/{len(items)} 个商品: {item_name}")
            print(f"商品ID: {snapshot['commodity_id']}")
            print(f"商品编号: {snapshot['commodity_no']}")
            print(f"当前价格: {current_price}")
            print(f"参考价格: {reference_price}")
            print(f"商品状态: {snapshot['state_text']}")
            print(f"商品页: {snapshot['market_url']}")
            print(f"价格走势页: {snapshot['price_trend_url'] or '未配置'}")

            write_price_log(
                item_name=item_name,
                commodity_id=snapshot["commodity_id"],
                commodity_no=snapshot["commodity_no"],
                current_price=current_price,
                reference_price=reference_price,
                state_text=snapshot["state_text"],
            )

            if current_price is None:
                print_status(f"[跳过] {item_name} 当前没有可用价格。")
                continue

            signals = build_triggered_signals(
                item_name=item_name,
                item=item,
                current_price=current_price,
                market_url=snapshot["market_url"],
                price_trend_url=snapshot["price_trend_url"],
            )
            if not signals:
                print_status(f"[正常] {item_name} 还没有触发任何提醒。")
                continue

            cooldown_minutes = get_item_cooldown_minutes(config, item)
            if should_notify(item_name, cooldown_minutes, notification_state):
                summary = "；".join(signal["message"] for signal in signals)
                print_status(f"[触发提醒] {summary}")
                send_desktop_notification("悠悠有品价格提醒", summary)
                perform_signal_actions(config, item_name, current_price, signals)
                mark_notified(item_name, notification_state)
            else:
                print_status(f"[已抑制重复提醒] {item_name} 仍满足条件，但还在冷却时间内。")

        except YoupinAuthError as exc:
            handle_auth_error(config, item, notification_state, exc)
        except requests.HTTPError as exc:
            print_status(f"[HTTP 错误] 第 {index} 个商品: {exc}")
        except requests.RequestException as exc:
            print_status(f"[请求失败] 第 {index} 个商品: {exc}")
        except Exception as exc:
            print_status(f"[异常] 第 {index} 个商品: {exc}")

        if index < len(items):
            time.sleep(request_pause_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="监控悠悠有品商品价格，触发阈值时发送提醒并执行辅助动作。"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="只执行一轮检查，方便测试配置是否正确。",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    notification_state: dict[str, float] = {}

    while True:
        try:
            # 每轮都重载配置，这样你改 config.json 切换监控饰品或 Cookie 后不用重启脚本。
            config = load_config()
            session = create_session(config)
            interval = int(config.get("check_interval_seconds", 300))

            print()
            print_status("开始新一轮检查...")
            check_items(config, session, notification_state)
            print_status("本轮检查结束。")

            if args.once:
                break

            print_status(f"休眠 {interval} 秒后继续...")
            time.sleep(interval)

        except KeyboardInterrupt:
            print_status("用户手动停止了脚本。")
            break
        except Exception as exc:
            print_status(f"程序运行失败: {exc}")
            if args.once:
                raise
            print_status("60 秒后自动重试。")
            time.sleep(60)


if __name__ == "__main__":
    main()
