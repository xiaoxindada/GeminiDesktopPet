# 标准版动作生成记录

使用内置 imagegen 工具。以 Assets/Standard/standee.png 为角色、比例和服饰参考，以用户提供的 Q 版动作截图为动作参考。
保留原待机、睡眠与贴边素材；新增动作按 PetState 的名称直接加载。

## 通用提示词

Use case: stylized-concept. Asset type: single full-body transparent PNG desktop pet action. Image 1 is exact STANDARD tall anime character identity/costume reference. Image 2 is Q-version action reference sheet ONLY, not body proportions. Recreate the same character with a clearly distinct semantic pose described below. Preserve normal tall anime proportions, violet-blue long hair fading pink, cat ears and fluffy tail, viewer-left pink eye and viewer-right amber eye, star hair accessory, exact navy/white/gold fantasy costume and white boots. Match polished original anime illustration, consistent head/body ratio and costume across actions. Entire ears, hair, hands, boots and tail within canvas with transparent padding, clean real alpha background. No text, no labels, no watermark, no panels. No extra limbs. Absolutely NO colored backdrop, NO aura, NO gradients around character, NO glow halo or shadow behind silhouette; outside character must be alpha 0. Action:

## 动作提示词

搜索动作最终改用 `Assets/Sprites/searching.png` 单图作姿势参考：直立，放大镜置于琥珀色眼睛前，另一只手靠近下巴。明确要求空白透明画布，不包含渐变背景或光晕。

- **idle-2**: Relaxed straight stance, hands gently clasped behind back, curious slight head tilt, soft smile.
- **idle-3**: Weight on one leg, both hands loosely clasped in front at waist, calm attentive face.
- **listening**: Lean forward slightly, cup one hand beside cat ear, other arm relaxed, attentive wide eyes.
- **thinking**: One hand supports opposite elbow, index finger rests below chin, glance upward thoughtfully, tail curls into question shape.
- **writing**: Write with pen on a small notebook supported in other hand, gaze down in concentration.
- **running**: Dynamic running stride in three-quarter side view, one knee lifted, opposite arms pumping, hair and tail trail behind.
- **searching**: Hold a magnifying glass before one eye, lean forward inquisitively, free hand on hip.
- **delegating**: Hold several task cards in raised hand, other hand gestures toward two tiny magical cat assistants at feet, confident expression.
- **happy**: Joyful closed-eye broad smile, both fists raised beside cheeks, buoyant stance.
- **pat**: Eyes peacefully closed, blush and delighted smile, head lowered to invite a head pat, hands clasped close to chest.
- **surprised**: Wide eyes, small open mouth, hands lifted with open palms near shoulders, slight backward lean.
- **hum**: Eyes closed in contentment, hands clasped at chest, gentle sideways sway as if humming.
- **stretch**: Both arms stretched high overhead, eyes closed and happy relaxed mouth, torso elongated, feet apart.
- **drawing**: Hold artist palette in one hand and raised paintbrush in other, vivid tiny paint ribbon, lively focused smile.
- **oops**: Embarrassed wide eyes, both hands partly cover mouth, shoulders raised, toes turned slightly inward.
- **proud**: Wink confidently, one hand on hip and other thumb pointing to herself, chest lifted.
- **tired**: Drooping ears, half closed eyes, shoulders slumped, arms hang tiredly, knees slightly bent, FRONT view.
- **yawn**: Eyes closed, one hand covering yawning mouth, other hand loosely at side, sleepy slouch.
- **compact**: Sit compactly with knees tucked on a small closed travel trunk, papers beside it, thoughtful expression.
- **float**: Float weightlessly with bent knees and arms gently spread, hair lifts upward in soft curls, tail curves, no ground.
- **asking**: Raise one index finger inquisitively, other palm faces up, tilted head and questioning expression.
- **greet_dawn**: Sleepy dawn greeting, small purple sleep cap with star tassel, rub one eye and softly wave with other hand.
- **greet_morning**: Bright morning smile, one hand waving beside face, other hand at waist, lively upright stance.
- **greet_noon**: Hold a small golden sandwich in both hands near mouth, friendly cheerful lunchtime expression.
- **greet_afternoon**: Hold a lavender teacup at chest height and saucer in other hand, warm welcoming smile.
- **greet_evening**: Cheerful evening greeting, one hand makes V sign next to head, other rests on hip, playful tilted stance.
- **greet_night**: Tender sleepy goodnight, hands pressed together beside cheek, head tilted onto them, soft half closed eyes.
- **bye**: Three-quarter turned away, look back over shoulder while waving goodbye, flowing hair and tail.
- **busy_left**: Facing viewer-left, hold open lavender laptop in left arm while typing with right hand, focused expression.
- **busy_right**: Facing viewer-right, hold open lavender laptop in right arm while typing with left hand, focused expression.
- **swamped_left**: Hurry toward viewer-left with one raised knee, balance open laptop and loose papers, visibly flustered face.
- **swamped_right**: Hurry toward viewer-right with one raised knee, balance open laptop and loose papers, visibly flustered face.
- **annoyed**: Arms firmly crossed, brows furrowed, ears angled outward, small annoyed frown.
- **puffed**: Puff cheeks, both fists planted on hips, stubborn stance, tail raised.
- **redhot**: Very frustrated blushing face, clenched fists lifted beside shoulders, ears back, tiny stylized steam wisps.
- **sulk**: Sit turned slightly away, knees drawn up with arms wrapped around them, pout and look aside.
- **dizzy**: Unsteady leaning stance, spiral dazed eyes, one hand touches forehead, other arm extends for balance.
- **coaxed**: Shy relieved smile and blush, one hand at chest and other fingers touching cheek, ears perk up.
- **forgiven**: Warm closed-eye forgiving smile, arms open welcomingly with palms up, relaxed graceful posture.
- **update**: Hold an upward-pointing glowing star arrow with one hand, other hand presents it, bright confident expression.
- **reading**: Read an open navy book held in both hands at chest height; look attentively down at its pages, feet naturally planted. Reference Assets/Sprites/reading.png for action.
