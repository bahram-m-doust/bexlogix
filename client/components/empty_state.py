# Purpose: Centralized contextual empty-state messages for all roles.
# Workflow Role: Improves UX guidance when operational datasets are empty.

from __future__ import annotations


def get_empty_state_message(role: str, context: str) -> str:
    normalized_role = str(role or "").strip().lower()
    normalized_context = str(context or "").strip().lower()

    mapping = {
        ("visitor", "no_assignments"): "مسیر شما برای این تاریخ هنوز منتشر نشده است. انتشار مسیر بر عهدهٔ مدیر است؛ می‌توانید تاریخ دیگری را هم ببینید.",
        ("supervisor", "no_assignments"): "برای این تاریخ تخصیصی ثبت نشده است. ساخت و انتشار مسیر بر عهدهٔ مدیر است.",
        ("telesales", "no_queue"): "✅ هیچ پیگیری در انتظار وجود ندارد. کار عالی!",
        ("manager", "no_due_stores"): "فروشگاهی در صف ویزیت وجود ندارد. فایل فروشگاه‌ها را بارگذاری کنید یا تاریخ دیگری را انتخاب کنید.",
        ("manager", "no_assignments"): "برای این تاریخ هنوز تخصیصی ثبت نشده است. ابتدا فایل وضعیت روزانه را اعمال و مسیر را بسازید.",
        ("manager", "no_pending_telesales"): "موردی در صف انتظار تماس تلفنی وجود ندارد.",
        ("manager", "no_route_for_visitor"): "برای ویزیتور انتخاب‌شده هنوز مسیری ثبت نشده است.",
        ("supervisor", "no_visits"): "برای این تاریخ هنوز نتیجه ویزیتی ثبت نشده است.",
        ("supervisor", "no_telesales"): "موردی در صف فروش تلفنی وجود ندارد.",
    }

    return mapping.get((normalized_role, normalized_context), "داده‌ای برای نمایش در این بخش وجود ندارد.")

