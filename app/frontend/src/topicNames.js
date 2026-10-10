/* A topic the clustering has not named yet carries a neutral birth label,
   "Topic 4558a7fe". It is an identifier, not a name, so a page does not set
   it as the topic's name (wave 8, N10). */
const UNNAMED = /^Topic [0-9a-f]{8}$/i;
export const UNNAMED_TOPIC_WORDS = 'A topic not yet named';
export const isUnnamedTopic = (title) => typeof title === 'string' && UNNAMED.test(title.trim());
