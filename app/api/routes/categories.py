"""Category routes — the editable, workspace-global industry list."""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.category import CategoryCreate, CategoryResponse, CategoryUpdate
from app.services import category_service
from app.services.category_service import CategoryAlreadyExistsError

router = APIRouter(prefix="/categories", tags=["categories"])


@router.get("", response_model=list[CategoryResponse], summary="List categories")
def list_categories(
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[CategoryResponse]:
    return [
        CategoryResponse.model_validate(c) for c in category_service.list_categories(db)
    ]


@router.post(
    "",
    response_model=CategoryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a category",
    responses={409: {"description": "A category with that name already exists"}},
)
def create_category(
    payload: CategoryCreate,
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CategoryResponse:
    try:
        category = category_service.create_category(db, payload.name)
    except CategoryAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A category with that name already exists",
        ) from exc
    return CategoryResponse.model_validate(category)


@router.patch(
    "/{category_id}",
    response_model=CategoryResponse,
    summary="Rename a category",
    responses={404: {"description": "Category not found"}},
)
def update_category(
    category_id: str,
    payload: CategoryUpdate,
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CategoryResponse:
    category = category_service.get_category(db, category_id)
    if category is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")
    try:
        category = category_service.update_category(db, category, name=payload.name)
    except CategoryAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A category with that name already exists",
        ) from exc
    return CategoryResponse.model_validate(category)


@router.delete(
    "/{category_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a category",
    responses={404: {"description": "Category not found"}},
)
def delete_category(
    category_id: str,
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    category = category_service.get_category(db, category_id)
    if category is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")
    category_service.delete_category(db, category)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
