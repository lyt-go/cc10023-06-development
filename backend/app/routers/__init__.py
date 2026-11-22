"""业务路由汇总：训练领域专用路由在前，通用资源动态路由在后。"""
from __future__ import annotations

from app.routers import resource, training_domain

ROUTERS = [training_domain, resource]
