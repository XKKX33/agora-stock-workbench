def test_sentiment_marks_unavailable_fields(client):
    response = client.get("/api/sentiment")

    assert response.status_code == 200
    payload = response.json()
    assert payload["industries"]
    news = payload["news_sentiment"]
    # 夹具库没登记任何舆情来源,要报"来源没登记",而不是给一份全 0 的分布
    assert news["availability"] == "unavailable"
    assert news["missing_reason"] == "no_source_registered"
    assert news["counts"] is None


def test_sentiment_industry_moneyflow_aggregates_by_industry(client):
    """行业资金流:按最新资金流交易日聚合,如实带出数据覆盖区间。"""
    response = client.get("/api/sentiment")
    assert response.status_code == 200
    mf = response.json()["industry_moneyflow"]
    assert mf["availability"] == "available"
    assert mf["reason"] is None
    # 种子库资金流只灌了最后 6 个交易日
    assert mf["as_of"] == "20250812"
    assert mf["date_range"] == ["20250805", "20250812"]
    assert mf["stock_count"] == 7
    assert len(mf["items"]) == 4  # 7 只股票分属 4 个行业

    top = mf["items"][0]
    # 半导体 3 只强主升股,每只净流入 +1000
    assert top["industry"] == "半导体"
    assert top["stock_count"] == 3
    assert top["net_mf_amount"] == 3000.0
    assert top["buy_lg_amount"] == 9000.0
    assert top["sell_lg_amount"] == 3000.0
    assert top["buy_elg_amount"] == 6000.0
    assert top["sell_elg_amount"] == 1500.0
    # 净流入降序:半导体排第一
    nets = [item["net_mf_amount"] for item in mf["items"]]
    assert nets == sorted(nets, reverse=True)
    # 行业覆盖:半导体/化工/公用事业/消费电子
    industries = {item["industry"] for item in mf["items"]}
    assert industries == {"半导体", "化工", "公用事业", "消费电子"}


def test_sentiment_industry_moneyflow_missing_table_stays_truthful(client):
    """资金流表缺失时如实报 unavailable,不返回全 0 的假聚合。"""
    from engine.db import Store

    repository = client.app.state.repository
    with Store(repository.db_path, ensure_schema=False) as store:
        store.con.execute("DROP TABLE moneyflow")
    payload = client.get("/api/sentiment").json()
    mf = payload["industry_moneyflow"]
    assert mf["availability"] == "unavailable"
    assert mf["reason"]
    assert mf["items"] == []


def test_factor_response_has_no_fake_ml_prediction(client):
    response = client.get("/api/factors/600001.SH")

    assert response.status_code == 200
    payload = response.json()
    ml = payload["machine_learning"]
    # 产物目录被 model_dir 夹具指到空目录,要报"没训练过"(not_trained),
    # 而不是含糊的"等一等"(pending)——后者的意思是"训练过但样本外不达标"。
    # 两种状态要分开,否则没人知道该去训练还是该去改因子。
    assert ml["availability"] == "not_trained"
    assert ml["reason"]
    # 没有产物时不该出现诊断段:空面板比没有面板更让人困惑
    assert "diagnostics" not in ml
    # 关键不变量:非 available 状态下一律不给预测值
    assert "probability" not in ml
    assert "prediction" not in ml
    assert "score" not in ml
    # 门槛要一起给出,否则"不达标"是个无法复核的结论
    assert ml["thresholds"]["min_ic"] > 0
    assert payload["factors"]


def _write_below_threshold_artifact(model_dir):
    """落一份"训过但样本外反着排"的产物。这是实测中真实出现的形态。"""
    import numpy as np

    from engine.ml import registry
    from engine.ml.model import make_model

    model = make_model("ridge")
    rng = np.random.default_rng(11)
    model.fit(rng.normal(size=(200, 2)), rng.normal(size=200))
    return registry.save_artifact(
        model,
        name="factor_ml",
        horizon="ret5",
        features=["f_a", "f_b"],
        metrics={
            "ic_mean": -0.1095,
            "ic_ir": -0.5506,
            "auc": 0.4689,
            "hit_rate": 0.4454,
            "n_days": 33,
            "n_samples": 8551,
            "monotonic": False,
            "train_ic": 0.1397,
            "overfit_gap": 0.2493,
            "daily_ic": [{"as_of": "20260609", "ic": 0.0305, "n": 260}],
            "buckets": [{"bucket": 1, "n": 1716, "avg_return": -0.0154}],
        },
        dataset={"replayed_days": 60, "n_rows": 15583, "label_cutoff": "20260724"},
        folds=[{"index": 0, "train_start": "20260428", "n_purged_days": 5, "metrics": {}}],
        params={"n_splits": 3, "stride": 1},
        base=model_dir,
    )


def test_pending_model_still_reports_diagnostics(client, model_dir):
    """未达门槛也要把体检数据给出来,但仍然一个预测值都不给。

    只回一句"模型不可用"等于什么都没说:用户需要看到 IC 是负的、
    过拟合缺口 0.25、分桶不单调,才知道下一步是换因子而不是等数据。
    """
    _write_below_threshold_artifact(model_dir)

    payload = client.get("/api/factors").json()
    ml = payload["machine_learning"]

    assert ml["availability"] == "pending"
    assert "低于门槛" in ml["reason"]
    # 达标项与不达标项都要能被页面逐条核对
    assert ml["metrics"]["n_days"] >= ml["thresholds"]["min_train_days"]
    assert ml["metrics"]["ic_mean"] < ml["thresholds"]["min_ic"]

    diag = ml["diagnostics"]
    assert diag["overfit_gap"] > 0.1
    assert diag["monotonic"] is False
    assert diag["daily_ic"] and diag["buckets"]
    assert diag["folds"][0]["n_purged_days"] == 5
    assert diag["params"]["n_splits"] == 3
    assert diag["dataset"]["label_cutoff"] == "20260724"
    # 权重绝不外传:前端用不到,传了只是多一个泄漏面
    assert "state" not in diag
    # pending 状态下依然不给任何预测
    assert "probability" not in ml
    assert "prediction" not in ml
    assert "score" not in ml


def test_ledger_summary_uses_only_filled_returns(client):
    response = client.get("/api/ledger/summary")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ret5"]["sample_count"] == 0
    assert payload["ret5"]["average"] is None


def test_ledger_returns_persisted_picks(client):
    response = client.get("/api/ledger")

    assert response.status_code == 200
    assert response.json()["items"]
