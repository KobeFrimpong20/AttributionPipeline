"""A small, fixed product catalog: collections of products with prices."""

from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal
from typing import Tuple

NUM_COLLECTIONS = 10
PRODUCTS_PER_COLLECTION = 20
PRICE_RANGE_CENTS = (500, 25_000)  # $5.00 to $250.00


@dataclass(frozen=True)
class Product:
    product_id: str
    collection_id: str
    price: Decimal


@dataclass(frozen=True)
class Collection:
    collection_id: str
    products: Tuple[Product, ...]


def build_catalog(
    num_collections: int = NUM_COLLECTIONS,
    products_per_collection: int = PRODUCTS_PER_COLLECTION,
    seed: int = 0,
) -> Tuple[Collection, ...]:
    """Builds the catalog. The same arguments always give the same catalog."""
    rng = random.Random(seed)
    collections = []
    for c in range(num_collections):
        collection_id = f"col_{c:02d}"
        products = tuple(
            Product(
                product_id=f"prod_{c:02d}_{p:02d}",
                collection_id=collection_id,
                price=Decimal(rng.randint(*PRICE_RANGE_CENTS)) / 100,
            )
            for p in range(products_per_collection)
        )
        collections.append(Collection(collection_id, products))
    return tuple(collections)


CATALOG: Tuple[Collection, ...] = build_catalog()


def pick_collection(rng: random.Random, catalog: Tuple[Collection, ...] = CATALOG) -> Collection:
    """Picks any collection."""
    return rng.choice(catalog)


def pick_product_in(rng: random.Random, collection: Collection) -> Product:
    """Picks a product from one collection."""
    return rng.choice(collection.products)


def pick_other_product(
    rng: random.Random, current: Product, catalog: Tuple[Collection, ...] = CATALOG
) -> Product:
    """Picks a product other than `current`, from anywhere in the catalog."""
    while True:
        product = pick_product_in(rng, pick_collection(rng, catalog))
        if product != current:
            return product
