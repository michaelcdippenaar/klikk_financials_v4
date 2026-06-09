from rest_framework import serializers

from .models import Category, ClassificationRule, TransactionClassification


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ['id', 'type', 'name', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']


class ClassificationRuleSerializer(serializers.ModelSerializer):
    category = CategorySerializer(read_only=True)
    category_id = serializers.PrimaryKeyRelatedField(
        queryset=Category.objects.all(), source='category', write_only=True,
    )

    class Meta:
        model = ClassificationRule
        fields = [
            'id', 'tag', 'transaction_type', 'category', 'category_id',
            'is_active', 'priority', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate_tag(self, value):
        return (value or '').strip().upper()


class TransactionClassificationSerializer(serializers.ModelSerializer):
    category = CategorySerializer(read_only=True)

    class Meta:
        model = TransactionClassification
        fields = [
            'id', 'transaction', 'category', 'rule', 'is_manual', 'source',
            'matched_tag', 'classified_at', 'created_at',
        ]
        read_only_fields = fields
