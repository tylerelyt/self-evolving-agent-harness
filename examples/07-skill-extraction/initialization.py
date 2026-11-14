"""Check the actual resource selection recorded at skill initialization."""
RESOURCES = {'scripts', 'references', 'assets'}


def resources_match(selection, created):
    if not isinstance(selection, list) or not isinstance(created, list):
        return False
    if any(not isinstance(x, str) for x in selection + created):
        return False
    return (len(selection) == len(set(selection)) and set(selection) <= RESOURCES
            and len(created) == len(set(created)) and 'agents' in created
            and set(created) - {'agents'} == set(selection))
