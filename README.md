# 公路病害桩号定位与供数服务

FastAPI + SQLite。桩号表按版本登记（版号与内容一经登记不可修改、不可删除），
各版共用同一条公路的实测路距轴。养护员从旧桩号表出发定位病害，确认后固定
实距区段；供数时按现行表分段换算回桩号，并返回所用表版号。

## 启动

```bash
docker compose up --build -d
```

* Swagger: <http://localhost:8000/docs>
* OpenAPI: <http://localhost:8000/openapi.json>
* 健康检查: <http://localhost:8000/health>

SQLite 文件在命名卷 `chain-data` 的 `/data/chains.db`，容器内路径由
`CHAIN_DB_PATH` 指定。

本地开发：

```bash
python3 -m pip install -r requirements-dev.txt
CHAIN_DB_PATH=./data/chains.db uvicorn app.main:app --reload
pytest -q
```

## 数据模型

| 表 | 说明 |
| --- | --- |
| `chains` | 公路；一条公路只有一条实测路距轴。 |
| `chain_versions` | 桩号表版本，`draft` / `active` / `superseded`；同公路同时只有一个 `active`。 |
| `chain_segments` | 某版分段：`start_m`、`end_m`（实测米数）与 `start_station`（段首桩号）。 |
| `damages` | 病害定位、候选、确认后的固定实距区段与确认版号。 |

约束由数据库与业务层双重保证：

* 分段 `end_m > start_m`，同版各段在实距上不得重叠，只允许端点相接。
* 触发器禁止 `UPDATE`/`DELETE` 既有分段，禁止修改版号、所属公路与登记时间。
* 段内桩号随米数 1:1 递增（`end_station = start_station + end_m - start_m`）；
  段与段之间可跳号、可重号，因此同一桩号可对应多个实距落点。
* 分段端点也参与换算：桩号恰在某段端点时，该端点就是一个落点。

## 接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/roads` | 登记公路 |
| GET | `/roads`、`/roads/{id}` | 查询公路 |
| POST | `/roads/{id}/versions` | 登记新版桩号表（可 `activate=true` 直接生效） |
| GET | `/roads/{id}/versions`、`/versions/{no}` | 查看版本与分段 |
| POST | `/roads/{id}/versions/{no}/activate` | 换版 |
| GET | `/roads/{id}/current` | 现行表 |
| POST | `/roads/{id}/damages` | 定位病害（返回候选） |
| GET | `/roads/{id}/damages`、`/{damage_no}` | 查看定位/确认状态 |
| POST | `/roads/{id}/damages/{damage_no}/confirm` | 人工选定候选并确认 |
| GET | `/roads/{id}/damages/{damage_no}/supply` | 供数：沿现行表分段返回桩号 + 版号 |

### 定位结果状态

* `pending_survey`：无长度吻合区段，待补测。此时不能确认。
* `needs_selection`：多个候选，须人工选定后确认。
* `ready`：唯一候选，可直接确认。
* `confirmed`：已确认，实距区段固定，不可再定位或改选。

确认时提交的 `start_m`/`end_m` 必须与某个候选完全一致，否则返回 422。

### 覆盖一致性

`confirm` 与 `activate` 都要求“生效后的现行表完整覆盖全部已确认区段”：

* 换版：新表若有缺口，返回 409 `coverage_gap`，`gaps` 列出每个病害号与
  缺失实距区间；整次操作不落任何改动（`activate=true` 直接登记同样回滚）。
* 确认：若所选实距区段未被现行表覆盖，同样返回 409，指出病害号与缺失实距。

## Swagger 试跑

### 1. 重号定位

1. `POST /roads`：`{"id":"S12","name":"示范公路"}`
2. `POST /roads/S12/versions`，`activate=true`：

   ```json
   {
     "label": "旧表（两段桩号重号）",
     "activate": true,
     "segments": [
       {"start_m": 0, "end_m": 1000, "start_station": 0},
       {"start_m": 1000, "end_m": 2000, "start_station": 0}
     ]
   }
   ```

3. `POST /roads/S12/damages`：

   ```json
   {"damage_no":"D-001","old_version":1,"start_station":1000,"end_station":500,"measured_length":500}
   ```

   返回 `needs_selection`，三个候选：`(500,1000)`、`(1000,1500)`、`(1500,2000)`。
   其中 `(1000,1500)` 的起点恰好是第 1 段止点 / 第 2 段起点。

4. `POST /roads/S12/damages/D-001/confirm`：`{"start_m":1000,"end_m":1500}`
5. `GET /roads/S12/damages/D-001/supply` 得到现行表分段桩号（含 `version_no`）。

### 2. 换版（含缺口拒绝）

1. 先做一次缺口换版：`POST /roads/S12/versions` 登记

   ```json
   {"label":"缺口感","segments":[
     {"start_m":0,"end_m":900,"start_station":0},
     {"start_m":1600,"end_m":2600,"start_station":1600}
   ]}
   ```

2. `POST /roads/S12/versions/2/activate` → `409`，`gaps` 为
   `[{"damage_no":"D-001","missing":[[1000,1500]]}]`。
3. 再登记一份完整覆盖的新表并 `activate=true`：

   ```json
   {"label":"新表","activate":true,"segments":[
     {"start_m":0,"end_m":800,"start_station":10000},
     {"start_m":800,"end_m":1500,"start_station":10200},
     {"start_m":1500,"end_m":2500,"start_station":10700}
   ]}
   ```

4. 再查 `GET /roads/S12/damages/D-001/supply`，`supply.version_no` 变为新版号，
   桩号按新表换算（1000–1500m → 桩号 10400–10900）。

## 测试

```bash
pytest -q
```

覆盖：分段校验、端点换算、重号/跳号候选枚举、容差、无解与多解状态、
确认不可变、换版覆盖缺口（含多病害与事务回滚）、供数分段与版号、
分段不可改的数据库约束、并发版号分配。

## 已知边界

* 桩号重合时按落点全部枚举；候选数取决于数据本身，不做上限裁剪。
* 长度吻合默认容差 `0.1` 米，可用 `tolerance_m` 收紧或放宽（`0` 为严格相等）。
* 换版只校验已确认区段，不校验 `pending_survey` / `needs_selection` 的病害。
* 不提供删除公路/版本/病害的接口，历史版本与确认记录长期保留。
