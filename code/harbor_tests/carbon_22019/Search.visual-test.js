/** Hidden functional contract for carbon-design-system/carbon#22019. */
import React from 'react';
import userEvent from '@testing-library/user-event';
import { render, screen } from '@testing-library/react';
import Search from './Search';

const prefix = 'cds';

describe('ExpandableSearch visual contract', () => {
  it('f2p_magnifier_uses_the_icon_tooltip_visual_variant', () => {
    const { container } = render(
      <Search labelText="Search" onExpand={() => {}} isExpanded={false} />
    );

    expect(
      container.querySelector(`.${prefix}--search-magnifier-tooltip`)
    ).toHaveClass(`${prefix}--icon-tooltip`);
  });

  it('p2p_collapsed_search_keeps_the_expand_button_state', () => {
    render(<Search labelText="Search" onExpand={() => {}} isExpanded={false} />);
    expect(screen.getAllByRole('button')[0]).toHaveAttribute(
      'aria-expanded',
      'false'
    );
  });

  it('p2p_collapsed_search_keeps_the_input_out_of_tab_order', () => {
    render(<Search labelText="Search" onExpand={() => {}} isExpanded={false} />);
    expect(screen.getByRole('searchbox')).toHaveAttribute('tabIndex', '-1');
  });

  it('p2p_expand_button_still_invokes_onExpand', async () => {
    const onExpand = jest.fn();
    render(<Search labelText="Search" onExpand={onExpand} isExpanded={false} />);
    await userEvent.click(screen.getAllByRole('button')[0]);
    expect(onExpand).toHaveBeenCalledTimes(1);
  });

  it('p2p_nonexpandable_search_keeps_a_tabbable_input', () => {
    render(<Search labelText="Search" />);
    expect(screen.getByRole('searchbox')).not.toHaveAttribute('tabIndex', '-1');
  });
});
