# CS Market Monitor

一个用于监控悠悠有品商品价格的 Python 脚本。它会定时请求商品数据，在满足你设置的买入/卖出条件时发送桌面提醒，并按需自动打开商品页、价格走势页和登录页，方便你快速处理。

## 当前功能

- 支持按 `template_id` 监控悠悠有品市场商品
- 支持读取 `queryOnSaleCommodityList` 新接口所需鉴权头
- 支持价格提醒规则：`buy_below`、`sell_above`、`sell_below`、`drop_below`
- 支持 `buy_price + max_drop` 止损提醒
- 触发信号后可自动打开商品页和价格走势页
- 登录失效时可提醒并自动打开登录页
- 每轮检测前都会重新读取 `config.json`，改配置后无需重启脚本
- 自动导出价格日志与最新信号

## 环境要求

- Python 3.10+
- Windows 桌面环境（桌面通知与剪贴板能力主要按 Windows 使用场景编写）

## 安装

```powershell
python -m pip install -r requirements.txt
```

## 配置

仓库中提供的是示例文件 `config.example.json`，真实配置文件 `config.json` 已加入 `.gitignore`，不会上传到 GitHub。

1. 复制 `config.example.json` 为 `config.json`
2. 将 `auth.authorization`、`auth.deviceid`、`auth.deviceuk`、`auth.uk` 替换为你自己账号抓包得到的值
3. 按需要修改 `items` 中的监控目标与价格规则

建议从这个请求里复制请求头字段：

```text
/api/homepage/pc/goods/market/queryOnSaleCommodityList
```

至少需要这些字段：

- `authorization`
- `deviceid`
- `deviceuk`
- `uk`

## 运行

先执行一次单轮检查，确认配置没有问题：

```powershell
python main.py --once
```

持续监控：

```powershell
python main.py
```

## 生成文件

- `price_log.csv`：价格检测日志
- `signal_log.csv`：触发提醒记录
- `latest_signal.json`：最近一次触发的信号

这些文件都是运行产物，默认不会提交到仓库。

## 注意事项

- 请不要把你真实的 `config.json`、Cookie 或鉴权头提交到公开仓库
- 这个脚本是“监控 + 提醒 + 打开页面”的辅助工具，不会自动下单
- 如果悠悠有品接口字段变动，可能需要同步调整 `main.py`
