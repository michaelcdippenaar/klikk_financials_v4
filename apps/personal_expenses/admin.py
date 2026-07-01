from django.contrib import admin

from .models import Category, ClassificationRule, TransactionClassification


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ['type', 'name', 'created_at']
    list_filter = ['type']
    search_fields = ['name']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(ClassificationRule)
class ClassificationRuleAdmin(admin.ModelAdmin):
    list_display = ['tag', 'transaction_type', 'category', 'is_active', 'priority']
    list_filter = ['category__type', 'transaction_type', 'is_active']
    search_fields = ['tag']
    raw_id_fields = ['category']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(TransactionClassification)
class TransactionClassificationAdmin(admin.ModelAdmin):
    list_display = ['transaction', 'category', 'source', 'is_manual', 'matched_tag', 'classified_at']
    list_filter = ['source', 'is_manual', 'category__type', 'category']
    search_fields = ['transaction__description', 'matched_tag']
    raw_id_fields = ['transaction', 'rule', 'category']
    readonly_fields = ['classified_at', 'created_at']
