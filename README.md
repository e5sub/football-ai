# AI足球赛事研判指挥舱

静态 GitHub Pages 客户端，包含：

- 500彩票网竞彩足球日常自动更新
- Interwetten五年历史联赛画像与500近期增量
- 多Agent圆桌、实时阵容新闻、爆冷防线、独立防平表决、主推与冷门比分脚本
- 自动生成的资深分析师式“一眼看懂”结论与历史研判回看
- 客户手动录入主队、客队和比赛时间；自动匹配正式竞彩或500完整指数赛事
- 浏览器本地数据缓存与离线查看

## 自动更新

GitHub Actions 在北京时间 08:15、14:15、18:15、22:15、00:15、02:15、04:15 运行 `tools/update_daily_data.py`。赛果刷新包含当天及最近日期，晚间完赛后会继续回填。抓取失败时保留上一版赛事数据，不会写入空列表。网页还会缓存最近一次成功数据，网络波动时继续展示本地缓存。

手动赛事先按球队名称与开赛时间匹配正式竞彩数据，再匹配每天生成的500完整指数目录。匹配成功时直接复用与正式赛事相同的赔率、联赛画像、比分矩阵和多Agent结论，并自动清除同场手动副本；确实找不到时才使用历史画像与公开资料摘要，且只能进入“观察”层，不包装成重点推荐。

防平由概率模型、球队状态和比分结构三个本地核心席独立表决。至少两个核心席支持才可正式防平；Interwetten、500平赔和联赛画像只作参考，不能单独改变结论。中间总结不是复读旧结论，而是根据支持席、反对席、联赛样本、比分脚本和阵容信息重新组织自然话术。

冷门Agent拥有正式主张权：球队反向画像、比分矩阵、联赛兑现率和高波动赛制形成独立共识后，可以推翻赔率第一顺位并输出“冷门主判”或“深冷试胆”。系统不会机械反买，也不会把所有冷门都解释成平局；同批赛事会进行横向仲裁，只保留证据较强的平局主张，其余降为观察或转向有数据支持的非平冷门。历史回看会保留基础热门、最终主方向、Agent支持席和实际赛果，供每日增量复盘。

赔率变化现在按版本保存：首次出现时记录初始赔率与初始研判，后续只对有效变化增加快照。最终主方向发生变化时，必须同时满足明显变盘门槛（单项SP变化至少0.10，或归一化概率变化至少2.5个百分点）以及至少两个独立核心Agent支持；否则只记录候选观点并维持原判。开赛前90分钟内进入临场锁定，比赛开始后不再用赛中赔率改写赛前结论。历史回看会显示初始、最新、候选未通过和临场锁定轨迹。

历史复盘口径分为两层：“主判命中”表示最终主方向直接命中；“执行命中”表示实际赛果落在明确展示的主选或防守次选内。次选命中会计入执行口径命中率，同时单独保留主判命中率，未明确写入执行建议的方向不会算命中。

推荐分为“重点、谨慎、观察、观点”四层。重点层要求概率优势、冷门压力、球队画像覆盖和信心同时过线；历史页单独展示重点执行命中率。样本不足、只有赔率没有球队画像、或手动赛事未找到正式数据时会自动降级，避免所有比赛都被当成同等强度的推荐。

主访问地址：`https://football.071717.xyz`


## 使用范围

仅提供体育数据分析、赛前研究与内容创作参考；不涉及赌博，不提供下注服务，不承诺结果。

## 数据库版、账号功能与 Docker 镜像

项目在保留 `data/*.json` 赛事分析源的基础上，增加：

- 邮箱注册、24 小时激活链接和会话登录
- 管理员后台：管理员会话、用户列表、用户激活/停用、一次性激活码生成与使用状态
- 管理员后台可手动触发赛事数据更新，更新本地 JSON 并同步 MySQL
- 用户自己的赛前执行记录，不会写入浏览器本地数据
- 同步足彩玩法记录：胜平负、让球胜平负、比分、总进球、半全场，并按玩法自动结算
- 赛事结果进入 `data/matches.json` 后，用户查看记录时自动结算命中、未命中和盈利
- MySQL 持久化，服务端计算收益，前端不能直接修改结算结果
- 写操作启用双重提交 CSRF 防护：同源 `SameSite=Strict` Cookie 与 `X-CSRF-Token` 请求头必须匹配

下注窗口打开或切换玩法时，会从服务器当前数据库快照读取最新赔率；本地 `update_daily_data.py` 更新后，胜平负等数据源实际提供的赔率会自动填入。若某个玩法没有被数据源提供，页面会提示暂无最新赔率，不会把其他玩法赔率误用过去。

### 构建并发布镜像

推送到 `main` 分支后，GitHub Actions 会自动构建并发布到 GitHub Container Registry：

```text
ghcr.io/<GitHub用户名或组织>/football-ai-command-center:latest
```

首次使用需要在仓库 Settings > Actions > General 中允许 GitHub Actions 创建和写入 packages。版本 tag（例如 `v1.0.0`）还会生成对应版本镜像标签。

GitHub Actions 不运行数据抓取和数据库同步，只负责构建镜像。数据更新在本地执行，本地脚本会同时更新 JSON 和 MySQL；JSON 仍会提交到 GitHub 做备份，但仅修改 `data/` 的提交不会触发镜像构建。修改代码、Docker 配置或推送版本 tag 时才会构建 GHCR 镜像。

### 本地更新数据

复制 `.env.example` 为 `.env`，填写本机可访问的 MySQL 地址，然后安装依赖并运行：

```powershell
copy .env.example .env
python -m pip install -r backend/requirements.txt
python tools/update_daily_data.py --history-days 10 --history-retention 400
```

脚本会自动读取项目根目录的 `.env`，更新四个 JSON 文件，并将当前赛事、历史赛果、分析归档和完整赛事目录写入 MySQL 的 `data_snapshots` 表。完成后提交数据文件即可触发镜像构建：

```powershell
git add data
git commit -m "data: refresh football matches"
git push
```

### 使用外部 MySQL 运行

项目的 Compose 文件不包含数据库容器。请先准备 MySQL，并创建数据库：

```sql
CREATE DATABASE football_ai CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

复制 `.env.example` 为 `.env`，把 `DATABASE_URL` 改成实际 MySQL 地址，然后运行：

```bash
copy .env.example .env
docker login ghcr.io
docker compose pull
docker compose up -d
```

`DATABASE_URL` 格式示例：`mysql+pymysql://用户名:密码@MySQL主机:3306/football_ai?charset=utf8mb4`。打开 `http://localhost:8000`。第一个注册账号自动成为管理员，使用该账号的邮箱和密码进入管理后台，可生成激活码、管理用户和重置管理员密码。后续账号默认为普通用户。注册后，开发环境会在页面显示激活链接，也可以在注册时填写管理员生成的激活码直接激活；生产环境应将 `COOKIE_SECURE=1` 配合 HTTPS 使用。`/api/admin/settle` 可供部署平台定时调用，页面打开记录时也会自动检查最新赛果。

应用首次启动会自动创建全部数据表，并兼容已有旧版本数据库，补充管理员字段和会话字段。SQLite 使用本地 `football_ai.db` 文件，数据库文件和表都会自动创建；MySQL 需要先创建数据库本身，应用启动时会自动创建表和索引：

```sql
CREATE DATABASE football_ai CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

两种连接配置：

```text
SQLite: sqlite:///./football_ai.db
MySQL:  mysql+pymysql://用户名:密码@主机:3306/football_ai?charset=utf8mb4
```

该功能仅用于体育研究记录和统计，不连接任何博彩平台、不处理充值提现，也不提供真实下注服务。
