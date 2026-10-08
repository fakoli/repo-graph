def checkout_total(items):
    """Compute the cart subtotal with a nested money helper."""
    def café_total(values):
        """Sum unit prices for the cart subtotal."""
        return sum(value["price"] for value in values)
    return café_total(items)


def checkout_preview(items):
    """Describe cart subtotal fields without computing them."""
    return "price"


def refund_balance(balance, amount):
    """Subtract a paid amount from a retained balance."""
    return balance - amount


def invalidate_cache(cache, key):
    """Invalidate cache entry by key."""
    del cache[key]
