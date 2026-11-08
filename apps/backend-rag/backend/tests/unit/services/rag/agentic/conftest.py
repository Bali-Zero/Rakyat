"""RAG tests inherit the credential-free environment from unit/services/conftest.py.

Keep the actual Settings singleton during collection. Replacing its module cache
entry poisons router fixtures that rely on the public model_dump() contract.
Any test-specific overrides must be scoped and restored by that test.
"""
