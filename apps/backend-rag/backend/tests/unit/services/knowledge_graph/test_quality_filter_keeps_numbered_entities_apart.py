from backend.services.knowledge_graph.extractor import (
    ExtractedEntity,
    ExtractedRelation,
    ExtractionResult,
)
from backend.services.knowledge_graph.ontology import EntityType, RelationType
from backend.services.knowledge_graph.quality_filter import KGQualityFilter


def _entity(entity_id: str, entity_type: EntityType, name: str) -> ExtractedEntity:
    return ExtractedEntity(
        id=entity_id,
        type=entity_type,
        name=name,
        mention=name,
    )


def _filter(
    entities: list[ExtractedEntity], relations: list[ExtractedRelation]
) -> ExtractionResult:
    return KGQualityFilter(infer_relationships=False).filter_result(
        ExtractionResult(chunk_id="synthetic-chunk", entities=entities, relations=relations)
    )


def test_keeps_numbered_articles_apart_and_preserves_relation_endpoint() -> None:
    pasal_ten = _entity("900001", EntityType.PASAL, "Pasal 10")
    pasal_eleven = _entity("900002", EntityType.PASAL, "Pasal 11")
    law = _entity("900003", EntityType.UNDANG_UNDANG, "UU No. 6 Tahun 2023")

    result = _filter(
        [pasal_ten, pasal_eleven, law],
        [
            ExtractedRelation(
                source_id=pasal_eleven.id,
                target_id=law.id,
                type=RelationType.PART_OF,
                evidence="Pasal 11 is part of the law",
            )
        ],
    )

    assert {entity.name for entity in result.entities} == {
        "Pasal 10",
        "Pasal 11",
        "UU No. 6 Tahun 2023",
    }
    assert [(relation.source_id, relation.target_id) for relation in result.relations] == [
        (pasal_eleven.id, law.id)
    ]


def test_keeps_laws_and_kbli_codes_with_different_numbers_apart() -> None:
    law_2023 = _entity("900010", EntityType.UNDANG_UNDANG, "UU No. 6 Tahun 2023")
    law_2022 = _entity("900011", EntityType.UNDANG_UNDANG, "UU No. 6 Tahun 2022")
    kbli_47111 = _entity("900012", EntityType.KBLI, "KBLI 47111")
    kbli_47112 = _entity("900013", EntityType.KBLI, "KBLI 47112")

    result = _filter([law_2023, law_2022, kbli_47111, kbli_47112], [])

    assert {entity.name for entity in result.entities} == {
        "UU No. 6 Tahun 2023",
        "UU No. 6 Tahun 2022",
        "KBLI 47111",
        "KBLI 47112",
    }


def test_merges_duplicate_names_and_remaps_the_dropped_relation() -> None:
    shorter = _entity("900020", EntityType.IZIN_USAHA, "PT Contoh Satu")
    longer = _entity("900021", EntityType.IZIN_USAHA, "PT. Contoh Satu")
    law = _entity("900022", EntityType.UNDANG_UNDANG, "UU No. 6 Tahun 2023")

    result = _filter(
        [shorter, longer, law],
        [
            ExtractedRelation(
                source_id=shorter.id,
                target_id=law.id,
                type=RelationType.PART_OF,
                evidence="PT Contoh Satu relates to the law",
            )
        ],
    )

    assert {entity.name for entity in result.entities} == {
        "PT. Contoh Satu",
        "UU No. 6 Tahun 2023",
    }
    assert [(relation.source_id, relation.target_id) for relation in result.relations] == [
        (longer.id, law.id)
    ]


def test_merges_equal_numbers_but_keeps_same_names_of_different_types_separate() -> None:
    pasal_title = _entity("900030", EntityType.PASAL, "Pasal 10")
    pasal_upper = _entity("900031", EntityType.PASAL, "PASAL 10")
    document = _entity("900032", EntityType.DOKUMEN, "Shared Record")
    permit = _entity("900033", EntityType.IZIN_USAHA, "Shared Record")

    result = _filter(
        [pasal_title, pasal_upper, document, permit],
        [
            ExtractedRelation(
                source_id=document.id,
                target_id=permit.id,
                type=RelationType.PART_OF,
                evidence="The document and permit share a name",
            )
        ],
    )

    assert len([entity for entity in result.entities if entity.type == EntityType.PASAL]) == 1
    assert [(relation.source_id, relation.target_id) for relation in result.relations] == [
        (document.id, permit.id)
    ]


def test_a_dropped_entity_goes_to_its_most_similar_survivor_not_the_first() -> None:
    # Both survivors are above the threshold for the dropped name; the second
    # one is the closer match.
    first = _entity("900040", EntityType.IZIN_USAHA, "Izin Usaha Perdagangan Besar Contoh")
    closer = _entity("900041", EntityType.IZIN_USAHA, "Izin Usaha Perdagangan Kecil Contoh")
    dropped = _entity("900042", EntityType.IZIN_USAHA, "Izin Usaha Perdagangan Kecil Conto")
    quality_filter = KGQualityFilter(infer_relationships=False)

    id_map = quality_filter._build_entity_id_map([first, closer, dropped], [first, closer])

    assert quality_filter._similarity(dropped.name, first.name) >= quality_filter.fuzzy_threshold
    assert id_map[dropped.id] == closer.id


def test_two_runs_on_one_filter_do_not_share_their_merges() -> None:
    quality_filter = KGQualityFilter(infer_relationships=False)
    law = _entity("900052", EntityType.UNDANG_UNDANG, "UU No. 6 Tahun 2023")
    quality_filter.filter_result(
        ExtractionResult(
            chunk_id="synthetic-chunk-1",
            entities=[
                _entity("900050", EntityType.IZIN_USAHA, "PT Contoh Satu"),
                _entity("900051", EntityType.IZIN_USAHA, "PT. Contoh Satu"),
            ],
            relations=[],
        )
    )

    # The same ids come back in the next chunk for two unrelated entities.
    second = quality_filter.filter_result(
        ExtractionResult(
            chunk_id="synthetic-chunk-2",
            entities=[
                _entity("900050", EntityType.PASAL, "Pasal 10"),
                _entity("900051", EntityType.PASAL, "Pasal 11"),
                law,
            ],
            relations=[
                ExtractedRelation(
                    source_id="900050",
                    target_id=law.id,
                    type=RelationType.PART_OF,
                    evidence="Pasal 10 is part of the law",
                )
            ],
        )
    )

    assert {entity.name for entity in second.entities} == {
        "Pasal 10",
        "Pasal 11",
        "UU No. 6 Tahun 2023",
    }
    assert [(relation.source_id, relation.target_id) for relation in second.relations] == [
        ("900050", law.id)
    ]
