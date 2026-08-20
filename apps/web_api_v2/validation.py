from django.conf import settings
from graphql import GraphQLError
from graphql.validation import ASTValidationRule


class MaxFieldSelectionsRule(ASTValidationRule):
    """A small deterministic complexity ceiling for the first schema.

    Field selections are a conservative complexity proxy until later schema
    fields supply domain-specific weights.
    """

    def __init__(self, context):
        super().__init__(context)
        self.selection_count = 0

    def enter_field(self, node, *_args):
        self.selection_count += 1
        if self.selection_count == settings.WEB_API_V2_MAX_FIELD_SELECTIONS + 1:
            self.context.report_error(
                GraphQLError(
                    'GraphQL operation is too complex.',
                    nodes=[node],
                    extensions={'code': 'VALIDATION_ERROR'},
                ),
            )
