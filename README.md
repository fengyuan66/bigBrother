This is Big Brother. It is an AI agent system that takes in context (resources) from Webcam VLM, Screenshare VLM, and browser data. All live by X second intervals. Big Brother is interfaced via an app. A watcher, light LLM actor is given access to these resources, and the context of what the user is doing, with the intention of keeping the user on-task. This light LLM actor will summarise the objective things relevant (matters under the scope of keeping the user on-task) in a way that is non-biased like its stating a fact (interprative content can come from VLM's output summaries), and will report back with boolean "true", content of relevant info (if there are things relevant to indicate the user is distracted), and boolean false if there are no info relevant to suggest the user is off-task. If true for Y turns (Y consequtive turns), the Main Processing Agent (MPA actor, heavier LLM with better logic) will trigger and translate the cited facts and evidence into actionable words (set an agenda). Then, a personality agent actor will articulate that communication agenda in a human-speaking like way to the user. The final spoken text can then be rendered to audio with ElevenLabs so the webapp can voice-project the intervention.

For instance: 

VLM -> "The person is wearing a black hoodie, with a blue surgical mask. The person is holding a mobile phone, with their gaze staring down at the phone. The user seems to be in a ambiently lit room with a white background and plants nearby. The user is sitting in an office chair

Watcher -> true, "The person is holding a mobile phone, with their gaze staring down at the phone"

VLM -> "The person is wearing a black hoodie, with a blue surgical mask. The person is holding up a mobile phone, potentially to take a selfie, with their gaze looking right in the direction of phone. The user seems to be in a ambiently lit room with a white background and plants nearby. The user is sitting in an offie chair and rotating around

Watcher -> true, "The person is holding up a mobile phone, potentially to take a selfie, with their gaze looking right in the direction of phone."

MPA -> Given: 

"The person is holding a mobile phone, with their gaze staring down at the phone"

"The person is holding up a mobile phone, potentially to take a selfie, with their gaze looking right in the direction of phone."

Intention: The user is studying math homework

Agenda: Tell the user to stop using their phone. However, I'm not sure if it is relevant to their maths homework, but most likely not

Personality agent -> "Hey! Get off the phone, unless you can explain why that would be helpful for your math homework

Environment notes:

- `BIG_BROTHER_PERSONALITY_MODEL` controls the final text-generation actor that turns an MPA agenda into the exact spoken line.
- `BIG_BROTHER_PERSONALITY_BRIEF` is the tuning hook for the speaking style and tone.
- `ELEVENLABS_API_KEY` and `BIG_BROTHER_ELEVENLABS_VOICE_ID` enable the final spoken line to be synthesized into audio for the webapp audio player.
