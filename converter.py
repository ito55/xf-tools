import argparse
import sys
from pathlib import Path
import re
import mido
from music21 import stream, harmony, note, meter, key, instrument, converter
from music21 import metadata
# ==============================================================================
# Constants
# ==============================================================================

# Based on the detailed Yamaha XF Chord Name specification for SysEx messages
XF_ROOT_DISP = {0: "bbb", 1: "bb", 2: "b", 3: "", 4: "#", 5: "##", 6: "###"}
XF_ROOT_NOTE = {1: "C", 2: "D", 3: "E", 4: "F", 5: "G", 6: "A", 7: "B"}
XF_CHORD_TYPES = {
    0x00: "",           # Maj
    0x01: "6",          # Maj6
    0x02: "maj7",       # Maj7
    0x03: "maj7(#11)",  # Maj7(#11)
    0x04: "add9",       # Maj(9)
    0x05: "maj9",       # Maj7(9)
    0x06: "6(9)",       # Maj6(9)
    0x07: "aug",        # aug
    0x08: "m",          # min
    0x09: "m6",         # min6
    0x0A: "m7",         # min7
    0x0B: "m7b5",       # min7b5
    0x0C: "m(add9)",    # min(9)
    0x0D: "m9",         # min7(9)
    0x0E: "m11",        # min7(11)
    0x0F: "m(maj7)",    # minMaj7
    0x10: "m(maj7,9)",  # minMaj7(9)
    0x11: "dim",        # dim
    0x12: "dim7",       # dim7
    0x13: "7",          # 7th
    0x14: "7sus4",      # 7sus4
    0x15: "7b5",        # 7b5
    0x16: "7(9)",       # 7(9)
    0x17: "7(#11)",     # 7(#11)
    0x18: "7(13)",      # 7(13)
    0x19: "7(b9)",      # 7(b9)
    0x1A: "7(b13)",     # 7(b13)
    0x1B: "7(#9)",      # 7(#9)
    0x1C: "maj7aug",    # Maj7aug
    0x1D: "7aug",       # 7aug
    0x1E: "1+8",        # 1+8
    0x1F: "1+5",        # 1+5
    0x20: "sus4",       # sus4
    0x21: "1+2+5",      # 1+2+5
    0x22: "N.C.",       # cc (No Chord)
}

XF_REHE_ID = 0x02
XF_REHE_BASE = {
    0: "Intro",
    1: "Ending",
    2: "Fill-in",
    3: "A", 4: "B", 5: "C", 6: "D", 7: "E", 8: "F", 9: "G", 10: "H",
    11: "I", 12: "J", 13: "K", 14: "L", 15: "M"
}

# ==============================================================================
# Builder Logic
# ==============================================================================

def _normalize_chord_figure(figure: str) -> str:
    """
    Normalizes a chord figure string to be compatible with music21.
    - Replaces 'b' with '-' for flats.
    - Maps common alternative chord names (e.g., 'add9' -> 'add2', 'm7(11)' -> 'm11').
    - Simplifies common enharmonic spellings (e.g., 'E#' -> 'F', 'Gbb' -> 'F').
    """
    if not figure or figure == "N.C.":
        return figure

    # --- 1. Simplify Enharmonic Note Names ---
    # This regex finds note names (A-G with sharps/flats) that are not part of a chord type (like 'm7b5').
    # It correctly handles root notes and bass notes in slash chords.
    def simplify_enharmonics(match):
        note_name = match.group(0)
        enharmonic_map = {
            "E#": "F", "B#": "C",
            "Fb": "E", "Cb": "B",
            "Dbb": "C", "Ebb": "D", "Gbb": "F", "Abb": "G", "Bbb": "A"
        }
        return enharmonic_map.get(note_name, note_name)

    figure = re.sub(r'\b[A-G](?:bb|##|b|#)\b', simplify_enharmonics, figure)

    # --- 2. Normalize Chord Types and Flat Symbols ---
    figure = figure.replace("add9", "add2")
    figure = figure.replace("m7(11)", "m11")
    figure = figure.replace("m(maj7,9)", "m(maj9)")

    # music21 uses '-' for flat and '--' for double-flat.
    return figure.replace("bb", "--").replace("b", "-")

def _get_title_from_midi(mf: mido.MidiFile) -> str | None:
    """
    Attempts to find the song title from a mido MidiFile object.
    It typically resides in the first 'track_name' meta message of the first track.
    """
    if not mf.tracks:
        return None
    for msg in mf.tracks[0]:
        if msg.type == 'track_name':
            return msg.name.strip()
    return None

def _parse_chords_from_midi(midi_path: Path, ticks_per_quarter: int, debug_mode: bool = False) -> list[harmony.ChordSymbol]:
    """
    Parses a MIDI file for chord symbols using the mido library.
    It looks for chords in two common places:
    1. Yamaha XF-style SysEx messages.
    2. Standard text, lyric, or marker meta-messages.
    """
    chords = []
    debug_log_buffer = []

    # --- XF Parsing Logic (kept local to this function) ---
    XF_META_HEADER = (0x43, 0x7B)
    XF_CHORD_ID = 0x01

    def _parse_xf_chord_sysex(chord_bytes: tuple[int, ...]) -> str | None:
        if not (2 <= len(chord_bytes) <= 4):
            return None

        cr, ct = chord_bytes[0], chord_bytes[1]
        bn = chord_bytes[2] if len(chord_bytes) >= 3 else 127

        type_str = XF_CHORD_TYPES.get(ct)
        if type_str is None:
            return None
        if type_str == "N.C.":
            return "N.C."

        def parse_note_byte(note_byte: int) -> str | None:
            if note_byte == 127:
                return None
            fff = (note_byte >> 4) & 0b0111
            nnnn = note_byte & 0x0F

            disp_str = XF_ROOT_DISP.get(fff)
            note_str = XF_ROOT_NOTE.get(nnnn)

            if disp_str is None or note_str is None:
                return None
            return f"{note_str}{disp_str}"

        root_str = parse_note_byte(cr)
        if root_str is None:
            return None

        bass_str = parse_note_byte(bn)

        chord_figure = f"{root_str}{type_str}"
        if bass_str and root_str != bass_str:
            chord_figure += f"/{bass_str}"

        return chord_figure

    try:
        mf = mido.MidiFile(str(midi_path))
        # If the chord file has a different TPQ, it might affect timing.
        # We'll use the one from the melody file, but warn the user.
        if mf.ticks_per_beat != ticks_per_quarter:
            print(f"  - Warning: TPQ mismatch. Melody file has {ticks_per_quarter}, chord file has {mf.ticks_per_beat}. Using melody's TPQ for timing.")
    except Exception as e:
        print(f"❌ Error: Failed to open or parse MIDI file with mido: {midi_path}. Details: {e}", file=sys.stderr)
        return []

    absolute_time_ticks = 0
    # mido.merge_tracks provides a single, time-ordered stream of all messages.
    for msg in mido.merge_tracks(mf.tracks):
        # msg.time is the delta time in ticks from the previous event.
        absolute_time_ticks += msg.time
        current_chord_text = None

        # --- Method 1: Yamaha XF SysEx Chord Events ---
        if msg.type == 'sequencer_specific' and len(msg.data) > 2 and msg.data[:2] == XF_META_HEADER:
            data = msg.data
            event_id = data[2]

            if debug_mode:
                debug_log_buffer.append(f"  - DEBUG [TICK {absolute_time_ticks}]: Found XF SysEx Event (ID: {event_id:02X})")

            if event_id == XF_CHORD_ID:
                # The actual data payload starts after the header and ID
                payload = data[3:]
                # Filter out the 7F terminators/separators
                chord_bytes = tuple(b for b in payload if b != 0x7F)

                if not chord_bytes:
                    continue

                # Parse the byte sequence according to the XF specification.
                current_chord_text = _parse_xf_chord_sysex(chord_bytes)

                if debug_mode:
                    if not current_chord_text:
                        debug_log_buffer.append(f"    - Failed to parse XF chord bytes: {chord_bytes}")
                    else:
                        debug_log_buffer.append(f"    - Parsed XF chord as '{current_chord_text}'")

        # --- Method 2: Standard Text/Lyric/Marker Meta-Events ---
        elif msg.is_meta and msg.type in ['text', 'lyrics', 'marker']:
            # The text attribute is 'name' for 'track_name' and 'text' for others.
            if msg.type == 'track_name':
                text = msg.name
            else:
                text = msg.text
            text = text.strip()

            if debug_mode and text:
                debug_log_buffer.append(f"  - DEBUG [TICK {absolute_time_ticks}]: Found text in '{msg.type}': '{text}'")

            if text:
                # Expanded regex to find chord-like strings.
                # This is more permissive and handles variations like "C_maj", "Gm7", "C(add9)" etc.
                # It also strips surrounding characters like brackets or spaces.
                # The core of the chord is captured in group 1.
                match = re.search(r'[^A-G]*([A-G][b#]?(?:maj|min|m|M|dim|aug|sus|add|[-_])?[0-9]*(?:\(.*\))?(?:/[A-G][b#]?)?)\b', text)

                if match:
                    parsed_text = match.group(1)
                    try:
                        # Validate that music21 can understand this chord text
                        harmony.ChordSymbol(parsed_text)
                        current_chord_text = parsed_text
                    except Exception:
                        if debug_mode:
                            debug_log_buffer.append(f"    - DEBUG: Text '{parsed_text}' looked like a chord but failed to parse.")
                        pass # Not a valid chord symbol, ignore.

        # --- If a chord was found, create the music21 ChordSymbol object ---
        if current_chord_text:
            try:
                if current_chord_text == "N.C.":
                    # music21 has a specific object for "No Chord"
                    cs = harmony.NoChord()
                else:
                    # Normalize the chord text for music21 compatibility (e.g., 'Gb' -> 'G-')
                    normalized_text = _normalize_chord_figure(current_chord_text)
                    cs = harmony.ChordSymbol(normalized_text)

                if cs:
                    # Set the position in quarter notes
                    cs.offset = absolute_time_ticks / ticks_per_quarter
                    chords.append(cs)
            except Exception as e:
                print(f"Warning: Could not create chord from text '{current_chord_text}'. Details: {e}", file=sys.stderr)

    # --- Final output based on findings ---
    if debug_mode:
        print("\n  --- Chord Parser Debug Log ---")
        if not debug_log_buffer:
            print("  (No relevant events found to log)")
            for log_entry in debug_log_buffer:
                print(log_entry)
    else:
        print(f"  - Scanned MIDI data and found {len(chords)} chord symbols.")
    return chords

def _parse_rehe_from_midi(midi_path: Path, ticks_per_quarter: int, debug_mode: bool = False) -> list[tuple[int, str]]:
    """
    Parses a MIDI file for XF Rehearsal Marks.
    Returns a list of (tick, mark_text) tuples.
    """
    rehearsal_marks = []
    XF_META_HEADER = (0x43, 0x7B)

    try:
        mf = mido.MidiFile(str(midi_path))
    except Exception as e:
        print(f"❌ Error: Failed to open or parse MIDI file with mido: {midi_path}. Details: {e}", file=sys.stderr)
        return []

    absolute_time_ticks = 0
    for msg in mido.merge_tracks(mf.tracks):
        absolute_time_ticks += msg.time

        if msg.type == 'sequencer_specific' and len(msg.data) > 2 and msg.data[:2] == XF_META_HEADER:
            data = msg.data
            event_id = data[2]

            if event_id == XF_REHE_ID:
                # Payload starts after ID. 
                # Spec: FF 7F 04 43 7B 02 rr
                # mido data: 43 7B 02 rr
                # So rr is at index 3.
                if len(data) > 3:
                    rr = data[3]
                    base_idx = rr & 0x0F
                    var_idx = (rr >> 4) & 0x07
                    
                    base_str = XF_REHE_BASE.get(base_idx, "?")
                    # var_idx: 0=None, 1=', 2='', etc.
                    var_str = "'" * var_idx
                    
                    mark_text = f"{base_str}{var_str}"
                    rehearsal_marks.append((absolute_time_ticks, mark_text))
                    
                    if debug_mode:
                         print(f"  - Found Rehearsal Mark at tick {absolute_time_ticks}: {mark_text} (rr={rr:02X})")

    return rehearsal_marks

def _parse_melody_with_mido(melody_midi_path: Path, ticks_per_quarter: int) -> list[note.Note]:
    """
    Parses a MIDI file for melody notes on channel 1 using mido for reliability.
    This is more robust than relying on music21's instrument partitioning.
    """
    notes = []
    mf = mido.MidiFile(str(melody_midi_path))
    open_notes = {}  # To track note_on events
    absolute_time_ticks = 0

    for msg in mido.merge_tracks(mf.tracks):
        absolute_time_ticks += msg.time
        if msg.is_meta:
            continue

        # Check for channel 1 (mido channels are 0-15)
        if hasattr(msg, 'channel') and msg.channel == 0:
            if msg.type == 'note_on' and msg.velocity > 0:
                open_notes[msg.note] = (absolute_time_ticks, msg.velocity)
            elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                if msg.note in open_notes:
                    start_tick, velocity = open_notes.pop(msg.note)
                    duration_ticks = absolute_time_ticks - start_tick
                    n = note.Note(msg.note)
                    n.offset = start_tick / ticks_per_quarter
                    n.duration.quarterLength = duration_ticks / ticks_per_quarter
                    notes.append(n)
    return notes

def create_lead_sheet(midi_path: Path, output_xml_path: Path):
    """
    Generates a MusicXML lead sheet from a single MIDI file.
    It extracts chords and assumes the melody is on channel 1.
    """
    # 1. Parse melody file to get timing info (ticks per quarter note) and musical data
    print("  - Parsing MIDI file for melody and metadata...")
    try:
        melody_mf = mido.MidiFile(str(midi_path))
        ticks_per_quarter = melody_mf.ticks_per_beat
        title = _get_title_from_midi(melody_mf)
        # Use music21's converter for high-level musical structure
        melody_score = converter.parse(str(midi_path))
    except Exception as e:
        print(f"❌ Error: Could not parse MIDI file {midi_path}. Details: {e}", file=sys.stderr)
        sys.exit(1)

    # 2. Extract melody notes from channel 1 (using mido for reliability)
    print("  - Extracting melody notes from channel 1...")
    extracted_melody_notes = _parse_melody_with_mido(midi_path, ticks_per_quarter)

    # 3. Extract chords from the chord file using the timing from the melody file
    print("  - Parsing for chord symbols...")
    extracted_chords = _parse_chords_from_midi(midi_path, ticks_per_quarter)
    if not extracted_chords:
        print("Warning: No chord symbols were found in the chord file.")

    # 4. Get metadata (Time Signature, Key Signature) from the melody
    ts = melody_score.getElementsByClass(meter.TimeSignature).first()
    ks = melody_score.getElementsByClass(key.KeySignature).first()

    # 5. Create the new lead sheet structure
    lead_sheet = stream.Score()
    output_part = stream.Part()
    if title:
        lead_sheet.metadata = metadata.Metadata()
        lead_sheet.metadata.title = title
    output_part.id = 'lead_sheet_part'

    # Insert metadata
    if ts: output_part.insert(0, ts)
    if ks: output_part.insert(0, ks)

    # 6. Insert chords and melody notes into the output part
    print(f"  - Merging {len(extracted_chords)} chords and {len(extracted_melody_notes)} melody notes...")
    for cs in extracted_chords:
        output_part.insert(cs.offset, cs)
    for n in extracted_melody_notes:
        output_part.insert(n.offset, n)

    # 6.5. Quantize the part to clean up durations and avoid MusicXML errors.
    # This is crucial for fixing "inexpressible duration" errors.
    output_part = output_part.quantize()

    # 7. Add the completed part to the score and write to file
    lead_sheet.insert(0, output_part)
    print(f"  - Writing to MusicXML file: {output_xml_path}")
    output_xml_path.parent.mkdir(parents=True, exist_ok=True)
    lead_sheet.write('musicxml', fp=str(output_xml_path))

# ==============================================================================
# Command-line execution logic
# ==============================================================================

def check_chords_in_file(file_path: Path):
    """Checks a single MIDI file for chord information and prints debug output."""
    print(f"Checking for chords in: {file_path}")
    if not file_path.exists():
        print(f"❌ Error: File not found at {file_path}", file=sys.stderr)
        sys.exit(1)
    try:
        # For checking, we just need the TPQ from the file itself.
        mf = mido.MidiFile(str(file_path))
        chords = _parse_chords_from_midi(file_path, mf.ticks_per_beat, debug_mode=True)
        if chords:
            print(f"\n✅ Found {len(chords)} chords in the file.")
        else:
            print("\nℹ️ No chord symbols were found in the file.")
    except Exception as e:
        print(f"\n❌ An error occurred while checking the file: {e}", file=sys.stderr)
        sys.exit(1)

def check_rehe_in_file(file_path: Path):
    """Checks a single MIDI file for Rehearsal Marks and prints them."""
    print(f"Checking for Rehearsal Marks in: {file_path}")
    if not file_path.exists():
        print(f"❌ Error: File not found at {file_path}", file=sys.stderr)
        sys.exit(1)
    try:
        mf = mido.MidiFile(str(file_path))
        marks = _parse_rehe_from_midi(file_path, mf.ticks_per_beat, debug_mode=True)
        if marks:
            print(f"\n✅ Found {len(marks)} rehearsal marks in the file.")
        else:
            print("\nℹ️ No rehearsal marks were found in the file.")
    except Exception as e:
        print(f"\n❌ An error occurred while checking the file: {e}", file=sys.stderr)
        sys.exit(1)

def run_lead_sheet_generation(input_file: Path, output_file: Path):
    """Runs the full lead sheet generation process."""
    print("Starting lead sheet generation...")
    print(f"  - Input MIDI:  {input_file}")
    print(f"  - Output file:   {output_file}")
    create_lead_sheet(input_file, output_file)
    print(f"\n✅ Successfully created lead sheet: {output_file.resolve()}")

def main():
    """
    Main function to parse command-line arguments and run the script.
    """
    parser = argparse.ArgumentParser(
        description="Tools for building a MusicXML lead sheet from MIDI files.",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""\
Examples:
  # Generate a lead sheet
  python converter.py --input input/raw.mid --output output/sheet.musicxml

  # Check a single MIDI file for chord information
  python converter.py --check-chords "raw.mid"
"""
    )
    parser.add_argument("--input", type=Path, help="Path to the input MIDI file (containing both chords and melody).")
    parser.add_argument("--output", type=Path, help="Path for the generated MusicXML file.")

    # Group for utility functions
    util_group = parser.add_argument_group('Utilities')
    util_group.add_argument("--check-chords", type=Path, help="Check a single MIDI file for chord information and exit.")
    util_group.add_argument("--check-rehe", type=Path, help="Check a single MIDI file for Rehearsal Marks and exit.")

    args = parser.parse_args()

    # --- Handle Chord Check Utility ---
    if args.check_chords:
        check_chords_in_file(args.check_chords)
        sys.exit(0)
    
    elif args.check_rehe:
        check_rehe_in_file(args.check_rehe)
        sys.exit(0)

    # --- Handle Lead Sheet Generation ---
    elif args.input and args.output:
        try:
            run_lead_sheet_generation(
                input_file=args.input,
                output_file=args.output
            )
        except Exception as e:
            print(f"\n❌ An unexpected error occurred: {e}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)
    else:
        # If no action is specified, print help.
        if not any(vars(args).values()):
             parser.print_help()
        else:
             print("For lead sheet generation, you must provide both --input and --output.", file=sys.stderr)
             print("Use --help for more options.", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
