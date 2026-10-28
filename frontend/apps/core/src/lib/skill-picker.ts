export interface ChatSkill {
  id: string;
  name: string;
  description: string;
  source: string;
}

export function matchingSkills(
  skills: readonly ChatSkill[],
  query: string,
): ChatSkill[] {
  const needle = query.trim().toLocaleLowerCase();
  return skills
    .filter((skill) =>
      !needle || `${skill.id} ${skill.name} ${skill.description} ${skill.source}`
        .toLocaleLowerCase()
        .includes(needle),
    )
    .slice(0, 8);
}
