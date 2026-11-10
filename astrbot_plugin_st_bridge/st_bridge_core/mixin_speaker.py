"""Speakers, origins, group nicknames and affinity application."""

import re

from .log import logger
from .platform_api import GROUP, PRIVATE, IncomingMessage, ReplyBatch
from .affinity_store import group_key, user_hash, user_key
from .mood_store import GLOBAL as MOOD_GLOBAL, OWNER as MOOD_OWNER
from .emotion_config import mood_settings, AFFINITY_MAX, AFFINITY_MIN, DEFAULT_ROLE_TAG, GROUP_TRACK, OWNER_TRACK, active_identity, affinity_settings, affinity_tier_name, affinity_track, default_affinity_settings
from .reply_texts import fill_nickname, texts_settings
from .st_injection import MACRO_DESCRIPTIONS, PresetBackups, build_macros, configured_emotions, public_entries
from .http_protocol import DEFAULT_MAX_REQUEST_TEXT_BYTES
from .bridge_types import _GroupSpeaker, _Origin, _Speaker, _console_error_type, escape_st_macros, escape_st_tags


# Default of the first tag of every group turn; the ST preset tells speakers apart by it (no tag = the owner).
# The live value is affinity.group.role_tag.
GROUP_ROLE_TAG = DEFAULT_ROLE_TAG


class SpeakerMixin:
    """Speakers, origins, group nicknames and affinity application."""

    def _affinity_settings(self) -> dict:
        store = self._emotion_store
        return affinity_settings(store.config) if store is not None else default_affinity_settings()

    def _group_role_tag(self) -> str:
        return self._affinity_settings()["group"]["role_tag"]

    def _nickname(self) -> str:
        """The nickname (and wake word) of the character in use."""
        store = self._emotion_store
        return active_identity(store.config if store is not None else {})["nickname"]

    def _reply_text(self, key: str, **values: str) -> str:
        """One fixed reply line from the `texts` config, with the nickname and any other fillers put in."""
        store = self._emotion_store
        text = fill_nickname(texts_settings(store.config if store is not None else {})[key], self._nickname())
        for name, value in values.items():
            text = text.replace("{" + name + "}", str(value))
        return text

    def _turn_macros(self, speaker: _Speaker | None, origin: _Origin | None = None, *, song: bool | None = None) -> dict | None:
        """This turn's `{{stbridge_*}}` values for the claim; never blocks the turn (None = send none)."""
        store = self._emotion_store
        if store is None:
            return None
        try:
            track = speaker.track if speaker is not None else OWNER_TRACK
            # The group nickname reaches ST here too ({{stbridge_speaker}}), so it gets the same macro and tag
            # escape (ST substitutes macros one after another, so a substituted value can be expanded again).
            label = escape_st_tags(escape_st_macros(origin.group_label)) if origin is not None and origin.group_label else ""
            allowed = song if song is not None else bool(speaker is not None and speaker.sing_allowed)
            return build_macros(
                store.config,
                track=track,
                value=speaker.value if speaker is not None else None,
                speaker_label=label,
                song_allowed=allowed,
                **self._mood_texts(),
            )
        except Exception as error:
            logger.warning(f"[STBridge] Macro values unavailable ({_console_error_type(error)})")
            return None

    def _mood_settings(self) -> dict:
        store = self._emotion_store
        return mood_settings(store.config) if store is not None else mood_settings({})

    def _mood_texts(self) -> dict:
        """The two mood macro values for this turn (the empty-record text when nothing is stored)."""
        moods = self._mood_store
        if moods is None:
            return {}
        settings = self._mood_settings()
        return {"mood": moods.text(MOOD_GLOBAL, settings), "owner_mood": moods.text(MOOD_OWNER, settings)}

    def _record_mood(self, parsed, *, owner: bool) -> None:
        """Remember a valid reply's closing emotion: always in the overall list, in the owner's only for the owner."""
        moods = self._mood_store
        if moods is None:
            return
        try:
            emotion = parsed.pairs[-1][2] if parsed.pairs else None
            if emotion is None:
                return
            store = self._emotion_store
            known = configured_emotions(store.config) if store is not None else []
            if emotion not in known:
                logger.info("[STBridge] Mood not recorded (emotion not configured)")
                return
            moods.record(emotion, (MOOD_GLOBAL, MOOD_OWNER) if owner else (MOOD_GLOBAL,))
        except Exception as error:
            logger.warning(f"[STBridge] Mood not recorded ({_console_error_type(error)})")

    def _injection_view(self) -> dict:
        """GET /v1/injection: the entry list plus the macro values for generations that do not come from QQ."""
        store = self._emotion_store
        if store is None:
            raise RuntimeError("console config unavailable")
        data = public_entries(store.config)
        speaker = None
        if self._operator_platform_id and self._operator_sender_id:
            speaker = self._speaker(
                self._operator_platform_id, self._operator_sender_id, "", group=False, song_permission=False
            )
        data["macros"] = self._turn_macros(speaker, None, song=False) or {}
        data["macro_help"] = dict(MACRO_DESCRIPTIONS)
        return data

    def _backup_st_preset(self, name: str, text: str) -> str:
        """POST /v1/injection/backup: keep the ST preset as it was before the extension changes it."""
        store = self._emotion_store
        if store is None:
            raise RuntimeError("console config unavailable")
        return PresetBackups(store.data_dir).save(name, text)

    def _owner_key(self) -> str | None:
        """The owner's affinity key: the operator's private-chat identity."""
        if not self._operator_platform_id or not self._operator_sender_id:
            return None
        return user_key(self._operator_platform_id, self._operator_sender_id)

    def _astrbot_owner_key(self) -> str | None:
        """The owner's key on AstrBot, whichever platform is active."""
        platform_id, sender_id = self._astrbot_operator()
        return user_key(platform_id, sender_id) if platform_id and sender_id else None

    def _speaker(
        self,
        platform_id: object,
        sender_id: object,
        nickname: object,
        *,
        group: bool = False,
        song_permission: bool = True,
    ) -> _Speaker | None:
        """The speaker with this turn's affinity writes.

        ``song_permission`` also decides (rolls) whether a song request is allowed and adds that
        answer as the last write, so ST and the bridge see the same result; callers that only
        read the affinity turn it off.
        """
        if not isinstance(sender_id, str) or not sender_id.strip():
            return None
        # The bot may give one person the same openid in private and group chat, so group
        # members are stored under their own "group:" key and never touch the owner's record.
        key = user_key(platform_id, sender_id)
        if group:
            key = group_key(key)
        name = nickname if isinstance(nickname, str) else ""
        track = GROUP_TRACK if group else OWNER_TRACK
        store = self._affinity_store
        if store is None:
            reason = self._song_check(None, track, key) if song_permission else None
            allowed = reason is None if song_permission else None
            return _Speaker(key, name, track, sing_allowed=allowed, sing_reason=reason)
        try:
            settings = self._affinity_settings()
            owner = affinity_track(settings, OWNER_TRACK)
            members = affinity_track(settings, GROUP_TRACK)
            if group:
                value = store.get(key, members["initial_value"], members["min"], members["max"])
                owner_key = self._owner_key()
                # Group turns keep the owner's value as background; members cannot change it.
                owner_value = (
                    store.get(owner_key, owner["initial_value"], owner["min"], owner["max"])
                    if owner_key is not None
                    else owner["initial_value"]
                )
                writes = [
                    {"path": owner["path"], "value": owner_value},
                    {"path": members["path"], "value": value},
                ]
            else:
                value = store.get(key, owner["initial_value"], owner["min"], owner["max"])
                writes = [
                    {"path": owner["path"], "value": value},
                    {"path": members["path"], "value": None},
                ]
            allowed = reason = None
            if song_permission:
                reason = self._song_check(value, track, key)
                allowed = reason is None
                writes.append({"path": self._song_permission_path(), "value": 1 if allowed else 0})
            return _Speaker(key, name, track, value, writes, allowed, reason)
        except Exception:
            return _Speaker(key, name, track)

    def _apply_affinity(self, speaker: _Speaker | None, update) -> None:
        """Store the reply's 好感度 change for the speaker; never blocks the reply."""
        store = self._affinity_store
        if store is None or speaker is None or speaker.value is None:
            return
        try:
            own = affinity_track(self._affinity_settings(), speaker.track)
            hashed = user_hash(speaker.key)
            if update is None:
                value = store.get(speaker.key, own["initial_value"], own["min"], own["max"])
                logger.info(
                    f"[STBridge] Affinity user={hashed} before={value} "
                    f"parsed=none applied=+0 after={value} track={speaker.track}"
                )
                return
            before = store.get(speaker.key, own["initial_value"], own["min"], own["max"])
            delta = update.resolve(before)
            change = store.apply_delta(
                speaker.key,
                delta,
                max_delta=own["max_delta"],
                initial_value=own["initial_value"],
                nickname=speaker.nickname,
                low=own["min"],
                high=own["max"],
            )
            logger.info(
                f"[STBridge] Affinity user={hashed} before={change.before} "
                f"parsed={delta:+d} applied={change.applied:+d} after={change.after} "
                f"track={speaker.track}"
            )
        except Exception as error:
            logger.warning(
                f"[STBridge] Affinity update failed ({_console_error_type(error)})"
            )

    def _message_speaker(
        self, msg: IncomingMessage, *, song_permission: bool = True
    ) -> _Speaker | None:
        if not msg.identity_ok:
            return None
        try:
            return self._speaker(
                msg.platform_id,
                msg.sender_id,
                msg.sender_name,
                group=msg.chat_type == GROUP,
                song_permission=song_permission,
            )
        except Exception:
            return None

    @staticmethod
    def _message_origin(msg: IncomingMessage) -> _Origin | None:
        if not msg.identity_ok:
            return None
        return _Origin(msg.platform_id, msg.sender_id, msg.sender_name)

    def _origin_speaker(
        self, origin: _Origin | None, *, song_permission: bool = True
    ) -> _Speaker | None:
        if origin is None:
            return None
        return self._speaker(
            origin.platform_id,
            origin.sender_id,
            origin.nickname,
            group=origin.group_label is not None,
            song_permission=song_permission,
        )

    @staticmethod
    def _compose_content(origin: _Origin | None, speaker: _Speaker | None, text: str, role_tag: str = GROUP_ROLE_TAG) -> str:
        """ST text for one turn; group turns get "【群友】昵称（n）[好感度:N]：" in front of every line.

        What the user typed (and the group member's name) is made inert for ST's macro engine first. A group
        message is cut into lines (empty ones dropped) and each line carries the full prefix, so a second line
        that starts with another speaker's name cannot pass as that speaker. A private message keeps its lines.
        """
        text = escape_st_tags(escape_st_macros(text))
        if origin is None or origin.group_label is None:
            return text
        # The group member's own value, never the owner's.
        affinity_tag = (
            f"[好感度:{speaker.value}]"
            if speaker is not None and speaker.value is not None
            else ""
        )
        prefix = f"{role_tag}{escape_st_tags(escape_st_macros(origin.group_label))}{affinity_tag}："
        return "\n".join(f"{prefix}{line}" for line in text.splitlines() if line.strip())

    def _content_fits(self, origin: _Origin | None, texts: list[str]) -> bool:
        content = self._compose_content(
            origin, self._origin_speaker(origin, song_permission=False), "\n".join(texts), self._group_role_tag()
        )
        try:
            return len(content.encode("utf-8")) <= DEFAULT_MAX_REQUEST_TEXT_BYTES
        except UnicodeEncodeError:
            return False

    def _is_operator_private_message(self, msg: IncomingMessage) -> bool:
        if not self._operator_platform_id or not self._operator_sender_id:
            return False
        return (
            msg.chat_type == PRIVATE
            and msg.platform_id == self._operator_platform_id
            and msg.sender_id == self._operator_sender_id
        )

    @staticmethod
    def _alphabetic_speaker_suffix(index: int) -> str:
        suffix = ""
        while index > 0:
            index, remainder = divmod(index - 1, 26)
            suffix = chr(ord("A") + remainder) + suffix
        return suffix

    def _allocate_group_fallback_label(self, reserved: set[str]) -> str:
        while True:
            index = self._next_group_fallback_index
            self._next_group_fallback_index += 1
            candidate = f"用户{self._alphabetic_speaker_suffix(index)}"
            if candidate not in reserved:
                return candidate

    def _group_speaker_label(self, sender_id: str, sender_name: object) -> str:
        display_name = sender_name if isinstance(sender_name, str) else ""
        display_name = re.sub(r"[\r\n]+", " ", display_name)
        # Nicknames must not be able to fake the leading role tag; clean before the slot logic.
        display_name = re.sub(r"[【】]", "", display_name).strip()

        speaker = self._group_speakers.get(sender_id)
        if speaker is None:
            speaker = _GroupSpeaker(order=self._next_group_speaker_order)
            self._next_group_speaker_order += 1
            self._group_speakers[sender_id] = speaker

        if display_name != speaker.display_name:
            speaker.display_name = display_name
        if display_name and display_name not in speaker.name_slots:
            slot = self._group_name_slot_counts.get(display_name, 0) + 1
            self._group_name_slot_counts[display_name] = slot
            speaker.name_slots[display_name] = slot

        ordered_speakers = sorted(
            self._group_speakers.items(),
            key=lambda item: item[1].order,
        )
        reserved_names = {
            current.display_name
            for _, current in ordered_speakers
            if current.display_name
        }
        labels: dict[str, str] = {}
        used_labels: set[str] = set()

        for current_id, current in ordered_speakers:
            if not current.display_name:
                continue

            slot = current.name_slots[current.display_name]
            base_label = f"{current.display_name}（{slot}）"
            candidate = base_label
            collision = 1
            while candidate in reserved_names or candidate in used_labels:
                candidate = f"{base_label}〔{collision}〕"
                collision += 1
            labels[current_id] = candidate
            used_labels.add(candidate)

        reserved = reserved_names | used_labels
        for current_id, current in ordered_speakers:
            if current.display_name:
                continue

            if (
                not current.fallback_label
                or current.fallback_label in reserved
            ):
                current.fallback_label = self._allocate_group_fallback_label(
                    reserved,
                )
            labels[current_id] = current.fallback_label
            reserved.add(current.fallback_label)

        return labels[sender_id]

    async def _cmd_affinity(self, msg: IncomingMessage):
        """Report the sender's own affinity in private or group chat."""
        speaker = self._message_speaker(msg, song_permission=False)
        if speaker is None or speaker.value is None:
            yield ReplyBatch.of_text("好感度功能当前不可用。")
            return
        settings = self._affinity_settings()
        value = speaker.value
        tier = affinity_tier_name(affinity_track(settings, speaker.track), value)
        yield ReplyBatch.of_text(f"{settings['display_name']}对你的好感度：{value}（{tier}）")

    async def _cmd_affinity_set(self, msg: IncomingMessage):
        store = self._affinity_store
        speaker = self._message_speaker(msg, song_permission=False)
        if store is None or speaker is None or not self._is_operator_private_message(msg):
            yield ReplyBatch.of_text("好感度设置命令未执行。")
            return
        text = str(msg.args).strip()
        if not text.isdigit() or not AFFINITY_MIN <= int(text) <= AFFINITY_MAX:
            yield ReplyBatch.of_text(f"请提供 {AFFINITY_MIN} 到 {AFFINITY_MAX} 之间的整数。")
            return
        number = store.set_value(speaker.key, int(text), nickname=speaker.nickname)
        settings = self._affinity_settings()
        tier = affinity_tier_name(settings, number)
        yield ReplyBatch.of_text(f"已将{settings['display_name']}对你的好感度设为 {number}（{tier}）。")
