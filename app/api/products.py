"""单品实时查询接口"""
import logging
from fastapi import APIRouter, HTTPException

from app.services.product_service import check_sku
from app.models.schemas import ProductQueryResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/products", tags=["Products"])


@router.get(
    "/{sku}",
    summary="实时查询单个商品的尺码库存",
    response_model=ProductQueryResponse,
)
def get_product(sku: str):
    """
    实时查询单个 SKU 的价格和各尺码库存状态（直接请求 Adidas 官网，不经过数据库缓存）。

    路径参数:
        sku: 商品 SKU，如 JR5408

    返回:
        商品名称、配色、价格、每个尺码的库存状态和数量
    """
    try:
        result = check_sku(sku)
        return result
    except Exception as e:
        logger.error(f"查询商品 {sku} 失败: {e}")
        raise HTTPException(status_code=404, detail=f"查询 {sku} 失败: {e}")
