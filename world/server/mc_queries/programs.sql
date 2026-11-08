-- tables: mc_programs
SELECT id, title, owner_agent_id AS "ownerAgentId", status,
       planning_card_minted AS "planningCardMinted",
       meeting_card_minted AS "meetingCardMinted",
       pieces_minted AS "piecesMinted",
       cr_requested AS "crRequested",
       piece_status AS "pieceStatus"
FROM mc_programs
ORDER BY id
