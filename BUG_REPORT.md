# Bug Report: Video Source List Auto-Scrolls to Top on Input Focus

## Summary
When clicking on the input field to edit a video source name in the Video Source Settings dialog, the list immediately scrolls to the top, losing the user's scroll position.

## Environment
- **Application**: Matrix Deploy GUI
- **Component**: Video Source Settings dialog - Name input field
- **Date Reported**: July 14, 2026
- **Reporter**: User

## Steps to Reproduce
1. Open the Matrix Deploy application
2. Open the "Video Source Settings" dialog
3. Scroll down to a video source that is not at the top of the list (e.g., "OR4 Source 1")
4. Click on the Name input field for that source to edit it
5. **Bug occurs immediately** - list scrolls to top

## Expected Behavior
When clicking on a source name input field, the list should:
- Maintain the current scroll position
- Keep the selected source visible in the viewport
- Allow the user to edit the name without losing context
- Only scroll if the input field would be hidden by the keyboard or other UI elements

## Actual Behavior
When clicking on a source name input field, the list:
- **Immediately** scrolls to the top (before any editing occurs)
- Loses the user's scroll position
- Makes the source being edited disappear from view
- Forces the user to scroll back down to see what they're editing

## Impact
- **Severity**: Medium-High
- **User Experience**: Poor - causes significant frustration when editing multiple sources in a long list
- **Workaround**: Manually scroll back to the source after clicking the input field (must be done before typing)

## Technical Details
The issue likely occurs because:
1. The input field's `focus` event is triggering an unwanted scroll behavior
2. The parent container or scroll area is responding to the focus event by scrolling to the top
3. Possible causes:
   - `scrollIntoView()` being called on the wrong element
   - Default browser/Qt scroll behavior on input focus
   - Event handler causing unintended scroll reset
   - Parent container layout recalculation on focus

## Suggested Fix
Implement one or more of the following solutions:
1. **Prevent default scroll behavior**: Disable automatic scrolling on input focus using `scrollIntoView: false` or equivalent
2. **Save and restore scroll position**: Capture scroll position before focus event and restore immediately after
3. **Use `scrollIntoView()` correctly**: If scrolling is needed, use `scrollIntoView({block: 'nearest'})` to minimize movement
4. **Investigate focus event handlers**: Check for any event listeners that might be triggering scroll reset
5. **CSS solution**: Use `scroll-behavior: smooth` and prevent parent container from responding to focus events

## Related Files
- `matrix_deploy/gui.py` - Main GUI implementation (likely contains Video Source Settings dialog)
- `matrix_deploy/config.py` - Configuration and data models
- `config/deploy_config.json` - Configuration data structure

## Screenshots
**Before clicking input field:**
- Shows "OR4 Source 1" visible in the list at the bottom
- User has scrolled down to this source

**After clicking input field:**
- List has jumped to the top
- "OR 2 Source 2" entries are now at the top
- "OR4 Source 1" is no longer visible (scrolled out of view)

## Video Reference
See attached video: `7-14-2026, 2-51-18 PM (edited) (1).webm`

## Priority
**Medium** - Does not prevent functionality but significantly impacts usability when managing multiple sources

## Status
🔴 **Open** - Awaiting fix
