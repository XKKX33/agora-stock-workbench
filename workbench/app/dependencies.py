from fastapi import Request


def get_repository(request: Request):
    return request.app.state.repository


def get_scan_manager(request: Request):
    return request.app.state.scan_manager


def get_pipeline_manager(request: Request):
    return request.app.state.pipeline_manager


def get_news_collect_manager(request: Request):
    return request.app.state.news_collect_manager


def get_scheduler(request: Request):
    return request.app.state.scheduler


def get_experiment_service(request: Request):
    return request.app.state.experiment_service


def get_agent_judge_manager(request: Request):
    return request.app.state.agent_judge_manager



def get_returns_service(request: Request):
    return request.app.state.returns_service