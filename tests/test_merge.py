from datetime import date

from sqlalchemy import select

from app.main import merge_people, undo_merge
from app.models import ContactMethod, ExternalIdentity, Interaction, MergeHistory, Person, Tag


def test_merge_preserves_methods_identities_interactions_and_tags(db):
    survivor = Person(display_name="Jordan Kim", primary_email="jordan@example.com")
    duplicate = Person(display_name="Jordan Kim", current_organization="Example LLC")
    survivor.methods.append(ContactMethod(
        method_type="email", value="jordan@example.com", normalized_value="jordan@example.com"
    ))
    duplicate.methods.append(ContactMethod(
        method_type="phone", value="757-555-0123", normalized_value="17575550123"
    ))
    duplicate.identities.append(ExternalIdentity(
        provider="linkedin", provider_record_id="https://linkedin.com/in/jordan",
        profile_url="https://linkedin.com/in/jordan", source_payload={}, record_hash="x" * 64,
    ))
    duplicate.interactions.append(Interaction(
        interaction_type="Meeting", interaction_date=date(2026, 7, 1), meaningful=True
    ))
    duplicate.tags.append(Tag(name="USNA alumni"))
    db.add_all([survivor, duplicate])
    db.commit()
    merge_people(db, survivor, duplicate)
    db.commit()
    merged = db.scalar(select(Person).where(Person.id == survivor.id))
    assert merged.current_organization == "Example LLC"
    assert {m.method_type for m in merged.methods} == {"email", "phone"}
    assert len(merged.identities) == 1
    assert len(merged.interactions) == 1
    assert {t.name for t in merged.tags} == {"USNA alumni"}


def test_merge_field_choice_and_undo_restore_both_records(db):
    survivor = Person(
        display_name="Jordan Kim",
        primary_email="old@example.com",
        current_organization="Old Co",
    )
    duplicate = Person(
        display_name="Jordan K.",
        primary_email="new@example.com",
        current_organization="New Co",
    )
    duplicate.methods.append(ContactMethod(
        method_type="phone", value="757-555-0123", normalized_value="17575550123"
    ))
    duplicate.interactions.append(Interaction(
        interaction_type="Meeting", interaction_date=date(2026, 7, 1), meaningful=True
    ))
    db.add_all([survivor, duplicate])
    db.commit()
    duplicate_id = duplicate.id

    merge_people(
        db,
        survivor,
        duplicate,
        selected_values={
            "display_name": "Jordan Kim",
            "primary_email": "new@example.com",
            "current_organization": "New Co",
        },
    )
    db.commit()
    history = db.scalar(select(MergeHistory))
    assert survivor.primary_email == "new@example.com"
    assert db.get(Person, duplicate_id) is None

    response = undo_merge(None, history.id, db, None)
    assert response.status_code == 303
    restored = db.get(Person, duplicate_id)
    assert restored.display_name == "Jordan K."
    assert restored.primary_email == "new@example.com"
    assert {method.normalized_value for method in restored.methods} == {"17575550123"}
    assert len(restored.interactions) == 1
    assert survivor.primary_email == "old@example.com"
    assert history.undone_at is not None
