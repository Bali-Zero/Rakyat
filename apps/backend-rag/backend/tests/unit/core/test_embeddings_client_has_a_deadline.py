"""OpenAI embeddings client configuration tests."""

from backend.core.embeddings import _OPENAI_EMBEDDINGS_TIMEOUT, EmbeddingsGenerator


def test_openai_client_uses_embeddings_timeout():
    generator = EmbeddingsGenerator(provider="openai", api_key="synthetic-embedding-key")

    assert generator.client._client.timeout == _OPENAI_EMBEDDINGS_TIMEOUT
    assert generator.client._client.timeout.read == 60.0
    assert generator.client._client.timeout.connect == 5.0


def test_openai_generator_keeps_current_provider_model_and_dimensions():
    generator = EmbeddingsGenerator(provider="openai", api_key="synthetic-embedding-key")

    assert generator.provider == "openai"
    assert generator.model == "text-embedding-3-small"
    assert generator.dimensions == 1536
